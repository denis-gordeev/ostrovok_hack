// Fetch streaming supports authenticated POSTs; EventSource only supports GET.
export async function readSSE(body, onEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  function dispatch(block) {
    let event = 'message';
    const data = [];
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
    }
    if (data.length) onEvent(event, JSON.parse(data.join('\n')));
  }
  try {
    while (true) {
      const {done, value} = await reader.read();
      buffer += decoder.decode(value, {stream: !done});
      buffer = buffer.replace(/\r\n/g, '\n');
      let end;
      while ((end = buffer.indexOf('\n\n')) !== -1) {
        dispatch(buffer.slice(0, end));
        buffer = buffer.slice(end + 2);
      }
      if (done) {
        if (buffer.trim()) dispatch(buffer);
        break;
      }
    }
  } finally {reader.releaseLock();}
}

export function textContent(content) {
  if (typeof content === 'string') return content;
  return (content || []).filter(b => b.type === 'text').map(b => b.text).join('');
}

export function isVisibleModelStream(metadata = {}) {
  if ((metadata.tags || []).includes('context_summary')) return false;
  return !metadata.langgraph_node || metadata.langgraph_node === 'agent';
}

export function humanText(message) {
  return textContent(message.content).split('\n\nКонтекст карты (служебные координаты):')[0];
}

export function mergeLayers(current, changes) {
  const layers = {...current};
  for (const [id, layer] of Object.entries(changes || {})) {
    if (layer === null) delete layers[id]; else layers[id] = layer;
  }
  return layers;
}

export function technicalPreview(value) {
  const text = JSON.stringify(value, (key, v) => {
    if (/authorization|api[_-]?key|password|secret|reasoning|artifact/i.test(key)) return undefined;
    return v;
  }, 2);
  return (text || '').slice(0, 12000);
}

export const toolLabels = {
  search_web: 'Ищу источники в интернете', compose_routes: 'Объединяю участки маршрута',
  plan_route: 'Сравниваю варианты маршрута',
  search_hotels_in_tatarstan: 'Подбираю отели и проверяю тишину',
  noise_in_kazan: 'Оцениваю шум по модели', events_in_kazan: 'Ищу события на даты поездки',
  kazan_event_details: 'Уточняю сведения о событии',
  search_osm: 'Ищу места поблизости', overpass_query: 'Проверяю объекты на карте',
  walking_route: 'Строю пеший маршрут', driving_route: 'Строю маршрут на автомобиле', transit_route: 'Подбираю варианты проезда',
  wikipedia_search: 'Ищу сведения в энциклопедии', wikipedia_article: 'Читаю статью и ссылки на изображения',
  update_map: 'Обновляю карту',
  read_geo_result: 'Восстанавливаю проверенные данные',
};

export function traceFromMessages(messages) {
  let start=0;
  messages.forEach((m,i) => {if (['human','user'].includes(m.type || m.role)) start=i;});
  const rows=new Map();
  for (const message of messages.slice(start)) {
    for (const call of message.tool_calls || []) rows.set(call.id, {
      id:call.id, name:call.name, label:toolLabels[call.name] || 'Проверяю данные',
      input:technicalPreview(call.args), status:'interrupted',
    });
    if ((message.type || message.role) === 'tool' && rows.has(message.tool_call_id)) {
      Object.assign(rows.get(message.tool_call_id), {
        status:message.status === 'error'?'error':'done', output:technicalPreview(message.content),
      });
    }
  }
  return [...rows.values()].slice(-100);
}
