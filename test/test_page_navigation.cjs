// 页面拆分回归：验证公共导航与聊天恢复的边界，不调用真实模型或数据库。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, '../web/static/js', name), 'utf8');
const common = read('common.js');
const chat = read('chat.js');

function navigation(view) {
  const stored = new Map();
  const events = [];
  const visits = [];
  const context = {
    accountKey:'zhiku.session.alice',
    state: {view, session:'old'},
    localStorage: {setItem:(key,value)=>stored.set(key,value),removeItem:key=>stored.delete(key)},
    crypto: {randomUUID:()=> 'new-session'},
    location: {assign:url=>visits.push(url)},
    Event: class {constructor(type){this.type=type;}},
    CustomEvent: class {constructor(type,options){this.type=type;this.detail=options.detail;}},
    window: {dispatchEvent:event=>events.push(event)},
    closeMenu(){}, renderSessions(){},
  };
  vm.createContext(context);
  vm.runInContext(common.slice(common.indexOf('function openSession('), common.indexOf('function syncSidebar(')),context);
  return {context,stored,events,visits};
}

function historyFixture() {
  const elements = new Map();
  const messages = [];
  const streams = [];
  const requests = [];
  const element = () => ({textContent:'',innerHTML:'',replaceChildren(){messages.length=0;},append(){}});
  const context = {
    accountKey:'zhiku.session.alice',
    state:{session:'current',sessions:[],loadVersion:0},
    $:selector=>{if(selector==='.message')return messages[0];if(!elements.has(selector))elements.set(selector,element());return elements.get(selector);},
    localStorage:{setItem(){}},
    setBusy:busy=>{context.state.busy=busy;},
    renderSessions(){},updateCount(){},scrollBottom(){},icons(){},
    emptyState:element,renderAttachments(){},footer(){},renderProgress(){},
    addMessage:(role,text)=>{const item={role,answer:text};messages.push(item);return item;},
    api:async url=>{requests.push(url);return {items:[],active_task:{task_id:'pending',status:'processing',query:'测试问题'}};},
    setTimeout:()=>1,clearTimeout(){},pollTask(){},finish(){},fail(){},
    streamText:text=>text,encodeURIComponent,
    EventSource:class {
      constructor(url){this.url=url;this.listeners={};streams.push(this);}
      addEventListener(name,listener){this.listeners[name]=listener;}
      close(){this.closed=true;}
    },
  };
  vm.createContext(context);
  vm.runInContext(chat.slice(chat.indexOf('function disconnect('),chat.indexOf('function scrollBottom(')),context);
  vm.runInContext(chat.slice(chat.indexOf('function connectStream('),chat.indexOf('function updateInput(')),context);
  return {context,messages,streams,requests};
}

(async()=>{
  // 导入页保存目标会话后跳转；聊天页只通知自身加载，不额外刷新页面。
  for(const view of ['import','chat']) {
    const f=navigation(view);
    f.context.openSession('selected');
    assert.equal(f.stored.get('zhiku.session.alice'),'selected');
    if(view==='import')assert.deepEqual(f.visits,['/chat.html']);
    else {assert.equal(f.events[0].type,'zhiku:open-session');assert.equal(f.events[0].detail,'selected');assert.equal(f.visits.length,0);}
    f.context.newChat();
    assert.equal(f.stored.has('zhiku.session.alice'),false);assert.equal(f.context.state.session,null);
    if(view==='chat')assert.equal(f.events[1].type,'zhiku:new-chat');
    else assert.equal(f.visits.length,2);
  }

  // 返回聊天页时恢复后台任务，切换会话后关闭旧订阅并忽略迟到的事件。
  let f=historyFixture();
  await f.context.loadHistory('returning');
  assert.equal(f.requests[0],'/history/returning');
  assert.equal(f.streams[0].url,'/stream/returning?task_id=pending');
  assert.equal(f.messages[0].role,'user');
  assert.equal(f.messages[0].answer,'测试问题');
  const oldAnswer=f.messages[1];
  await f.context.loadHistory('another');
  assert.equal(f.streams[0].closed,true);
  f.streams[0].listeners.delta({data:JSON.stringify({delta:'旧事件'})});
  assert.equal(oldAnswer.answer,'');
  assert.equal(f.streams[1].url,'/stream/another?task_id=pending');

  // 已完成的任务只展示历史；旧请求晚返回时不能覆盖新会话。
  f=historyFixture();
  let release;
  f.context.api=url=>url.endsWith('/old')?new Promise(resolve=>{release=resolve;}):Promise.resolve({items:[{role:'assistant',text:'新会话答案'}],active_task:{status:'completed'}});
  const old=f.context.loadHistory('old');
  await f.context.loadHistory('new');
  release({items:[{role:'assistant',text:'旧会话答案'}]});await old;
  assert.equal(f.context.state.session,'new');
  assert.equal(f.messages.length,1);
  assert.equal(f.messages[0].answer,'新会话答案');
  assert.equal(f.streams.length,0);
  console.log('页面导航、任务恢复和旧请求隔离检查通过。');
})().catch(error=>{console.error(error);process.exitCode=1;});
