// 会话删除回归测试：通过页面和接口替身验证交互，不删除真实聊天记录。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '../web/static/js/app.js'), 'utf8');
// 只加载删除相关函数，避免执行页面初始化时的网络请求和事件绑定。
const functions = script.slice(script.indexOf('function requestDeleteSession('), script.indexOf('function setView('));

function setup() {
  // 每个场景使用独立状态；记录接口调用，模拟弹窗开关及删除后的页面变化。
  const elements = new Map();
  const state = {session:'current',sessions:[{session_id:'current',title:'Current'},{session_id:'other',title:'Other'}],sessionsVersion:0};
  const calls = [];
  const context = {state, encodeURIComponent,
    $: selector => {
      if (!elements.has(selector)) elements.set(selector, {textContent:'',hidden:false,showModal(){this.open=true;},close(){this.open=false;state.deleteTarget=null;}});
      return elements.get(selector);
    },
    $$: () => [], api: async (...args) => calls.push(args), toast: () => {},
    newChat: () => {state.session='new';}, renderSessions: () => {}, loadSessions: () => {},
  };
  vm.createContext(context);vm.runInContext(functions,context);
  return {context,state,calls,elements};
}
const confirm = {submitter:{value:'confirm'},preventDefault(){}};

(async () => {
  // 1. 取消不发请求；确认删除其他会话时，当前会话保持不变。
  let fixture = setup();
  fixture.context.requestDeleteSession('other');
  assert.equal(fixture.elements.get('#delete-session-title').textContent,'Other');
  await fixture.context.deleteSession({submitter:{value:'cancel'}});
  assert.equal(fixture.calls.length,0);
  await fixture.context.deleteSession(confirm);
  assert.equal(fixture.calls[0][0],'/history/other');
  assert.equal(fixture.calls[0][1].method,'DELETE');
  assert.equal(fixture.state.session,'current');
  assert.deepEqual(fixture.state.sessions.map(item=>item.session_id),['current']);

  fixture = setup();
  // 2. 删除当前会话后进入新会话。
  fixture.context.requestDeleteSession('current');
  await fixture.context.deleteSession(confirm);
  assert.equal(fixture.state.session,'new');

  fixture = setup();
  fixture.context.requestDeleteSession('other');
  // 3. 删除失败时保留会话和弹窗，并恢复操作状态以便重试。
  fixture.context.api = async () => {throw new Error('Deletion failed');};
  await fixture.context.deleteSession(confirm);
  assert.equal(fixture.state.sessions.length,2);
  assert.equal(fixture.elements.get('#confirm-dialog').open,true);
  assert.equal(fixture.elements.get('#delete-error').textContent,'Deletion failed');
  assert.equal(fixture.state.deleting,false);

  fixture = setup();
  // 4. 当前会话正在处理时，不允许打开删除确认。
  fixture.state.busy=true;
  fixture.context.requestDeleteSession('current');
  assert.equal(fixture.state.deleteTarget,undefined);

  fixture = setup();
  fixture.context.requestDeleteSession('other');
  // 5. 手动延迟接口返回，验证连续点击不会重复提交或替换删除目标。
  let resolve;
  fixture.context.api = (...args) => {fixture.calls.push(args);return new Promise(done=>{resolve=done;});};
  const pending=fixture.context.deleteSession(confirm);
  await fixture.context.deleteSession(confirm);
  fixture.context.requestDeleteSession('current');
  assert.equal(fixture.calls.length,1);
  assert.equal(fixture.state.deleteTarget,'other');
  resolve();await pending;
  assert.equal(fixture.state.session,'current');
  console.log('Session deletion: target isolation, cancel, failure, busy guard and duplicate submission passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
