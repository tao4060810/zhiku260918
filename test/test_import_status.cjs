// 导入锁定回归：使用接口和弹窗替身，覆盖跨页面恢复、批次结束和异步查询竞态。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../web/static/js/import-status.js'),'utf8');

function fixture() {
  const elements = new Map();
  const listeners = {};
  const requests = [];
  const messages = [];
  const context = {
    state:{},
    authReady:{then:fn=>fn(true)},
    $: selector => {
      if (!elements.has(selector)) elements.set(selector, {
        open:false,innerHTML:'',textContent:'',hidden:false,listeners:{},
        showModal(){this.open=true;},close(){this.open=false;},
        addEventListener(type,fn){this.listeners[type]=fn;},
      });
      return elements.get(selector);
    },
    api: (path,options) => new Promise((resolve,reject) => requests.push({path,options,resolve,reject})),
    window:{addEventListener:(name,fn)=>listeners[name]=fn,dispatchEvent(){}},
    CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},
    AbortSignal:{timeout:()=>({})},setTimeout:()=>1,clearTimeout(){},
    escapeHTML:value=>String(value??'').replaceAll('<','&lt;'),
    labels:{node_entry:'文件识别'},statusNames:{pending:'排队中',processing:'处理中',completed:'已完成',failed:'失败'},
    toast:message=>messages.push(message),
  };
  vm.createContext(context);
  vm.runInContext(source,context);
  return {context,requests,listeners,messages,dialog:context.$('#import-status-dialog'),elements};
}
const task = (id,status) => ({task_id:id,filename:`${id}.md`,status,running_list:[],done_list:[],error:status==='failed'?'解析失败':null});
const settle = () => new Promise(resolve=>setImmediate(resolve));

(async () => {
  const f = fixture();
  assert.equal(f.dialog.open,true,'初次进入时先确认后台任务');
  f.requests.shift().resolve({items:[task('a','pending'),task('b','processing')]});
  await settle();
  assert.equal(f.context.isImportLocked(),true);
  assert.match(f.context.$('#import-status-list').innerHTML,/a.md/);
  let prevented=false;
  f.dialog.listeners.cancel({preventDefault(){prevented=true;}});
  assert.equal(prevented,true,'Esc 不得提前解除锁定');

  let poll = f.context.refreshImportTasks();
  f.requests.shift().resolve({items:[task('a','completed'),task('b','processing')]});
  await poll;
  assert.equal(f.dialog.open,true,'部分完成时仍保持锁定');
  assert.match(f.context.$('#import-status-list').innerHTML,/已完成/);
  poll = f.context.refreshImportTasks();
  f.requests.shift().reject(new Error('离线'));
  await poll;
  assert.equal(f.dialog.open,true,'查询失败不代表任务结束');
  assert.equal(f.context.$('#import-status-error').hidden,false);
  // 终止当前批次只调用取消任务接口，不退出账号。
  const cancel = f.context.cancelImport();
  const cancelRequest = f.requests.shift();
  assert.equal(cancelRequest.path,'/tasks/b/cancel');
  assert.equal(cancelRequest.options.method,'POST');
  assert.equal(f.context.$('#import-cancel').disabled,true);
  cancelRequest.resolve({status:'canceling'});
  await cancel;
  f.requests.shift().resolve({items:[task('a','completed'),task('b','canceling')]});
  await settle();
  assert.equal(f.dialog.open,true,'后台清理完成前继续等待');
  assert.equal(f.context.$('#import-cancel').disabled,true);
  poll = f.context.refreshImportTasks();
  f.requests.shift().resolve({items:[task('a','completed'),task('b','canceled')]});
  await poll;
  assert.equal(f.dialog.open,false);
  assert.match(f.messages.at(-1),/已终止 1 份导入/);
  // 独立批次继续验证失败提示，终止提示不会覆盖不同批次的结果。
  const failedFixture = fixture();
  failedFixture.requests.shift().resolve({items:[task('c','processing')]});
  await settle();
  poll = failedFixture.context.refreshImportTasks();
  failedFixture.requests.shift().resolve({items:[task('c','failed')]});
  await poll;
  assert.equal(failedFixture.dialog.open,false,'全部结束后恢复，失败也不能永久锁住界面');
  assert.match(failedFixture.messages.at(-1),/1 份文档失败/);

  // 旧轮询即使晚返回，也不能覆盖上传响应中新创建的任务。
  poll = f.context.refreshImportTasks();
  const stale = f.requests.shift();
  f.context.beginImportUpload();
  f.context.updateImportUpload(100);
  assert.match(f.context.$('#import-status-description').textContent,/等待服务器/);
  f.context.acceptImportTasks(['new'],[{name:'新文件.md'}]);
  f.context.finishImportUpload();
  stale.resolve({items:[]});
  await poll;
  assert.equal(f.dialog.open,true);
  f.requests.shift().resolve({items:[task('new','pending')]});
  await settle();
  assert.equal(f.dialog.open,true,'上传结束后仍需等待后台解析');
  poll = f.context.refreshImportTasks();
  f.requests.shift().resolve({items:[task('new','completed')]});
  await poll;
  assert.equal(f.dialog.open,false);

  // 从浏览器往返缓存恢复时重新确认状态，避免使用离开前的空闲状态。
  f.listeners.pagehide();
  f.listeners['zhiku:restore']();
  assert.equal(f.dialog.open,true);
  f.requests.shift().resolve({items:[]});
  await settle();
  assert.equal(f.dialog.open,false);

  // 上传失败后仍会确认服务器有没有创建任务，确认没有后恢复文件选择。
  f.context.beginImportUpload();
  f.context.finishImportUpload();
  f.requests.shift().resolve({items:[]});
  await settle();
  assert.equal(f.dialog.open,false);
  // 传输阶段取消：先通知后端，再断开连接；等待确认，不能直接关闭弹窗。
  f.context.beginImportUpload();
  let aborted=false;
  f.context.state.uploadId='upload-1';
  f.context.state.uploadXHR={abort(){aborted=true;f.context.finishImportUpload();}};
  const cancelUpload=f.context.cancelImport();
  const uploadRequest=f.requests.shift();
  assert.equal(uploadRequest.path,'/uploads/upload-1/cancel');
  assert.equal(aborted,false);
  uploadRequest.resolve({});
  await cancelUpload;
  assert.equal(aborted,true);
  assert.equal(f.dialog.open,true);
  while(f.requests.length)f.requests.shift().resolve({items:[]});
  await settle();
  assert.equal(f.dialog.open,false);

  // 终止接口失败时保留任务及错误反馈，允许再次终止。
  poll=f.context.refreshImportTasks();
  f.requests.shift().resolve({items:[task('retry','processing')]});
  await poll;
  const retry=f.context.cancelImport();
  f.requests.shift().reject(new Error('网络中断'));
  await retry;
  f.requests.shift().resolve({items:[task('retry','processing')]});
  await settle();
  assert.equal(f.dialog.open,true);
  assert.match(f.messages.at(-1),/终止失败/);
  assert.equal(f.context.$('#import-cancel').disabled,false);
  console.log('导入状态测试通过：进度锁定、批次完成/失败、断线恢复、旧请求隔离、上传失败恢复。');
})().catch(error=>{console.error(error);process.exitCode=1;});
