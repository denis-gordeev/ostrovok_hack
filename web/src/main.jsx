import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {marked} from 'marked';
import DOMPurify from 'dompurify';
import {MapView} from './MapView.jsx';
import {readSSE, textContent, humanText, mergeLayers, technicalPreview, toolLabels, traceFromMessages, isVisibleModelStream} from './stream.js';
import './style.css';

const THREAD_KEY = 'ostrovok-thread';
const LEVEL_KEY = 'ostrovok-log-level';
async function api(path, body, signal) {
  const response = await fetch(path, {method:body === undefined?'GET':'POST', credentials:'same-origin',
    headers:body === undefined?{}:{'Content-Type':'application/json'},
    body:body === undefined?undefined:JSON.stringify(body), signal});
  if (!response.ok) throw new Error(response.status === 401 ? 'Нужно войти в приложение.' : 'Не удалось выполнить запрос. Попробуйте ещё раз.');
  return response;
}

function Markdown({text}) {
  const html = DOMPurify.sanitize(marked.parse(text || ''), {USE_PROFILES:{html:true}, FORBID_TAGS:['img','video','audio','iframe','style','form','input']});
  return <div className="markdown" dangerouslySetInnerHTML={{__html:html}} />;
}

function App() {
  const [thread, setThread] = useState(() => localStorage.getItem(THREAD_KEY));
  const [messages, setMessages] = useState([]), [layers, setLayers] = useState({}), [camera, setCamera] = useState(null);
  const [logs, setLogs] = useState([]), [debug, setDebug] = useState([]);
  const [level, setLevel] = useState(() => localStorage.getItem(LEVEL_KEY) || 'brief');
  const [input, setInput] = useState(''), [busy, setBusy] = useState(false), [loading, setLoading] = useState(true);
  const [error, setError] = useState(''), [selection, setSelection] = useState(null), [streamText, setStreamText] = useState('');
  const [streamingMessageId, setStreamingMessageId] = useState(null);
  const run = useRef(null), abort = useRef(null), transcript = useRef(null), textarea = useRef(null);
  const activeMessage = useRef(null), chunks = useRef(new Map());

  function progress(id, patch) {
    setLogs(previous => {
      const index = previous.findIndex(row => row.id === id);
      if (index === -1) return [...previous, {id, ...patch}].slice(-100);
      return previous.map((row,i) => i === index?{...row,...patch}:row);
    });
  }
  function receiveMessage(message, partial=false) {
    const content = textContent(message.content);
    const type = message.type || message.role;
    if (type === 'tool') {
      progress(message.tool_call_id, {status:message.status === 'error'?'error':'done', output:technicalPreview(message.content)});
      return;
    }
    if (type !== 'ai' && type !== 'assistant' && type !== 'AIMessageChunk') return;
    const calls = message.tool_calls || [];
    for (const call of calls) progress(call.id, {label:toolLabels[call.name] || 'Проверяю данные', name:call.name, input:technicalPreview(call.args), status:'running'});
    if (partial) {
      if (!content) return;
      const id = message.id || 'stream';
      const text = (chunks.current.get(id) || '') + content;
      chunks.current.set(id, text);
      activeMessage.current = id;
      setStreamingMessageId(id);setStreamText(text);
    } else {
      if (content) setMessages(previous => {
        const id=message.id || crypto.randomUUID();
        return [...previous.filter(m => m.id !== id), {...message,id}];
      });
      if (message.id === activeMessage.current || calls.length || content) {
        setStreamText('');setStreamingMessageId(null);
      }
    }
  }
  function onEvent(event, data) {
    if (event === 'metadata') run.current = {...run.current, id:data.run_id};
    if (event === 'error') throw new Error('Агент не смог завершить запрос. Попробуйте уточнить задачу.');
    if (event === 'debug') {
      if (['summarize','remember_results'].includes(data.payload?.name)) return;
      setDebug(previous => [...previous, {id:crypto.randomUUID(), event:data.type || event, text:technicalPreview(data)}].slice(-150));
    }
    if (event === 'messages') {
      const [message, metadata] = data;
      if (message && isVisibleModelStream(metadata)) receiveMessage(message, true);
    }
    if (event === 'updates') for (const update of Object.values(data)) {
      if (!update || typeof update !== 'object') continue;
      if (update.map_layers) setLayers(previous => mergeLayers(previous, update.map_layers));
      if (update.map_camera) setCamera(update.map_camera);
      for (const message of update.messages || []) receiveMessage(message);
    }
  }

  async function restore(id) {
    const response = await api(`/threads/${id}/state`);
    const state = await response.json();
    const values=state.values || {};
    setMessages((values.messages || []).filter(m => ['human','user','ai','assistant'].includes(m.type || m.role) && textContent(m.content)));
    setLayers(values.map_layers || {});setCamera(values.map_camera || null);
    setLogs(traceFromMessages(values.messages || []));
    return state;
  }
  useEffect(() => {
    if (!thread) {setLoading(false);return;}
    restore(thread).catch(() => {
      setError('Не удалось восстановить диалог. Обновите страницу или начните новый.');
    }).finally(() => setLoading(false));
  }, []);
  useEffect(() => {localStorage.setItem(LEVEL_KEY,level);}, [level]);
  useEffect(() => {
    if (transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
  }, [messages, streamText, logs.length]);

  async function send(event) {
    event.preventDefault();
    const query=input.trim();if (!query || busy || loading) return;
    setError('');setBusy(true);setInput('');setLogs([]);setDebug([]);setStreamText('');setStreamingMessageId(null);
    chunks.current = new Map();
    const messageId=crypto.randomUUID();
    let id=thread;
    const context=selection?`\n\nКонтекст карты (служебные координаты): ${selection.lat}, ${selection.lon}. Это выбранная пользователем точка, используй её для «здесь» и «этой точки».`:'';
    setMessages(previous => [...previous,{id:messageId,type:'human',content:query+context}]);
    abort.current=new AbortController();run.current={thread:id};
    try {
      if (!id) {
        const response=await api('/threads',{});
        id=(await response.json()).thread_id;setThread(id);localStorage.setItem(THREAD_KEY,id);
      }
      run.current={thread:id};
      const response=await api(`/threads/${id}/runs/stream`,{
        assistant_id:'geo_agent', input:{messages:[{id:messageId,role:'human',content:query+context}]},
        config:{max_concurrency:1},
        stream_mode:['updates','messages-tuple','debug'],
        on_disconnect:'cancel',
      },abort.current.signal);
      await readSSE(response.body,onEvent);
    } catch (exc) {
      if (exc.name !== 'AbortError') setError(exc.message);
    } finally {
      run.current=null;
      if (id) try {await restore(id);} catch {setError('Не удалось обновить состояние диалога. Обновите страницу.');}
      setLogs(previous => previous.map(row => row.status === 'running'?{...row,status:'interrupted'}:row));
      setBusy(false);setStreamText('');setStreamingMessageId(null);textarea.current?.focus();
    }
  }
  async function stop() {
    const current=run.current;
    if (!current?.id) return;
    try {await api(`/threads/${current.thread}/runs/${current.id}/cancel?wait=true&action=interrupt`,{});abort.current?.abort();}
    catch {setError('Не удалось остановить запрос.');}
  }
  function newChat() {
    if (busy) return;
    localStorage.removeItem(THREAD_KEY);setThread(null);setMessages([]);setLayers({});setCamera(null);
    setLogs([]);setDebug([]);setError('');setInput('');
    setSelection(null);
  }
  async function editLayer(layer, remove=false) {
    if (busy || !thread) return;
    const changes = {[layer.id]:remove?null:{...layer,visible:!layer.visible}};
    try {await api(`/threads/${thread}/state`,{values:{map_layers:changes}});setLayers(previous => mergeLayers(previous,changes));}
    catch (exc) {setError(exc.message);}
  }
  const hasAnswer = streamText && !messages.some(m => m.id === streamingMessageId);
  const lastHumanIndex=messages.findLastIndex(m => ['human','user'].includes(m.type || m.role));
  const lastAnswerIndex=messages.findLastIndex((m,i) => i > lastHumanIndex && ['ai','assistant'].includes(m.type || m.role) && !(m.tool_calls || []).length);
  const finalAnswer=lastAnswerIndex >= 0 ? messages[lastAnswerIndex] : null;
  const renderMessage=message => <article className={'message '+(['human','user'].includes(message.type || message.role)?'human':'assistant')} key={message.id}>
    <span className="message-label">{['human','user'].includes(message.type || message.role)?'ВЫ':'ГЕОАГЕНТ'}</span>
    {['human','user'].includes(message.type || message.role)?<p>{humanText(message)}</p>:<Markdown text={textContent(message.content)} />}
  </article>;
  return <div className="app-shell">
    <header className="topbar"><div className="brand"><span className="brand-mark">К</span><div><strong>Геоагент</strong><span>Исследуем Казань</span></div></div>
      <span className="prototype-badge">ПРОТОТИП</span><button className="new-chat" disabled={busy} onClick={newChat}>＋ Новый диалог</button></header>
    <main>
      <MapView layers={layers} camera={camera} busy={busy} selectedPoint={selection} onVisibility={editLayer} onRemove={layer => editLayer(layer,true)} onSelect={setSelection} />
      <section className="chat-panel">
        <div className="chat-header"><div><span className="eyebrow">ВАШ ПОМОЩНИК</span><h2>Спросите о городе</h2></div><label className="log-level">Подробность шагов<select aria-label="Подробность логов" value={level} onChange={e => setLevel(e.target.value)}>
          <option value="brief">Кратко</option><option value="detailed">Подробно</option><option value="debug">Отладка</option>
        </select></label></div>
        <div className="transcript" ref={transcript} aria-live="polite" aria-busy={busy}>
          {!messages.length && !loading && <div className="welcome"><span className="welcome-icon">↗</span><h3>Город — на одной карте</h3><p>Найдите интересные места, сравните маршруты или попросите добавить новый слой.</p>
            {['Покажи парки в центре Казани','Найди отели рядом с Казанским кремлём','Построй пеший маршрут от Кремля до улицы Баумана'].map(query => <button className="suggestion" key={query} onClick={() => {setInput(query);textarea.current?.focus();}}>{query}<span>↗</span></button>)}
          </div>}
          {loading && <p className="muted">Восстанавливаю диалог…</p>}
          {messages.filter((_,i) => i !== lastAnswerIndex).map(renderMessage)}
          {(busy || logs.length > 0 || debug.length > 0) && <section className="activity">
            <div className="activity-header"><strong>{busy?'Работаю над запросом':'Ход выполнения'}</strong><span>{level==='brief'?'Основные действия':level==='detailed'?'Инструменты и результаты':'События выполнения'}</span></div>
            {!logs.length && busy && <p className="step"><span className="spinner" />Разбираю запрос…</p>}
            {logs.map(row => <div className={'step-row '+row.status} key={row.id}><div className="step"><span>{row.status==='running'?<span className="spinner" />:row.status==='done'?'✓':row.status==='error'?'!':'—'}</span>{row.label || 'Обрабатываю результат'}{row.status==='error' && ' — не удалось получить данные'}{row.status==='interrupted' && ' — прервано'}</div>
              {level !== 'brief' && <details><summary>{row.name || 'Инструмент'} · параметры и результат</summary><pre>{row.input || ''}{row.output?`\n\nРезультат:\n${row.output}`:''}</pre></details>}
            </div>)}
            {level==='debug' && <details className="debug-events"><summary>События LangGraph ({debug.length})</summary>{debug.map(event => <details key={event.id}><summary>{event.event}</summary><pre>{event.text}</pre></details>)}</details>}
          </section>}
          {finalAnswer && renderMessage(finalAnswer)}
          {hasAnswer && <article className="message assistant"><span className="message-label">ГЕОАГЕНТ</span><Markdown text={streamText} /></article>}
          {error && <div className="error" role="alert">{error}</div>}
        </div>
        <form className="composer" onSubmit={send}>
          {selection && <div className="selection-tag">● Выбрана точка на карте <button type="button" aria-label="Сбросить выбранную точку" onClick={() => setSelection(null)}>×</button></div>}
          <textarea ref={textarea} value={input} disabled={loading} onChange={e => setInput(e.target.value)} placeholder="Что найдём или покажем на карте?" rows={3} aria-label="Сообщение агенту"
            onKeyDown={e => {if(e.key==='Enter' && !e.shiftKey){e.preventDefault();send(e);}}} />
          <div className="composer-footer"><span>Enter — отправить · Shift + Enter — новая строка</span>{busy?<button type="button" className="send" onClick={stop}>■ Стоп</button>:<button type="submit" className="send" disabled={!input.trim() || loading}>Отправить ↑</button>}</div>
        </form>
        <p className="footer-note">Ответы основаны на доступных данных. Время в пути может быть расчётным.</p>
      </section>
    </main>
  </div>;
}

createRoot(document.getElementById('root')).render(<App />);
