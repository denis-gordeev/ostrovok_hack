import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readSSE, humanText, mergeLayers, technicalPreview, isVisibleModelStream} from '../src/stream.js';

test('only agent model output is shown; internal summaries stay hidden', () => {
  assert.equal(isVisibleModelStream({langgraph_node:'summarize'}),false);
  assert.equal(isVisibleModelStream({tags:['context_summary']}),false);
  assert.equal(isVisibleModelStream({langgraph_node:'agent'}),true);
});

test('SSE handles arbitrary UTF-8 chunks, comments, CRLF and multiple events',async () => {
  const wire=new TextEncoder().encode(': keepalive\r\n\r\nevent: updates\r\ndata: {"text":"Казань"}\r\n\r\nevent: metadata\ndata: {"run_id":"r1"}\n\n');
  const body=new ReadableStream({start(controller){for(const byte of wire) controller.enqueue(new Uint8Array([byte]));controller.close();}});
  const events=[];await readSSE(body,(event,data) => events.push([event,data]));
  assert.deepEqual(events,[['updates',{text:'Казань'}],['metadata',{run_id:'r1'}]]);
});

test('map state updates remove layers and preserve unrelated ones',() => {
  assert.deepEqual(mergeLayers({a:{id:'a'},b:{id:'b'}},{a:null,c:{id:'c'}}),{b:{id:'b'},c:{id:'c'}});
});

test('technical views omit credentials and reasoning; human view omits map coordinates',() => {
  assert.equal(humanText({content:'Найди парк\n\nКонтекст карты (служебные координаты): 55, 49'}),'Найди парк');
  const preview=technicalPreview({tool:'search',api_key:'secret',reasoning_content:'hidden',artifact:{geometry:'huge'}});
  assert.equal(preview,'{\n  "tool": "search"\n}');
});
