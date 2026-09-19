// 认证前端：CSRF、401 解锁、跨标签账号切换与旧响应隔离。
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../web/static/js/common.js'),'utf8');
const authCode=source.slice(source.indexOf('const accountKey='),source.indexOf('function renderSessions('));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(){
 const elements=new Map(),events={},requests=[],redirects=[],stored=new Map(),classes=new Set(['auth-pending']);
 const state={sessionsVersion:0,loadVersion:0,stream:{close(){this.closed=true;}}};
 const el=selector=>{if(!elements.has(selector))elements.set(selector,{replaceChildren(){this.cleared=true;},removeAttribute(){this.cleared=true;},close(){this.closed=true;}});return elements.get(selector);};
 const context={state,Headers,document:{body:{dataset:{userId:'alice'},classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}}},
  localStorage:{getItem:key=>stored.get(key),setItem:(k,v)=>stored.set(k,v)},
  fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})),
  $:el,$$:()=>[el('dialog')],clearTimeout(){},Event:class{constructor(type){this.type=type;}},
  window:{addEventListener:(name,fn)=>events[name]=fn,dispatchEvent:e=>events[e.type]?.()},
  location:{replace:path=>redirects.push(path)},crypto:{randomUUID:()=> 'epoch'},toast(){},FormData:class{},
 };
 vm.createContext(context);vm.runInContext(authCode,context);
 return {context,requests,events,elements,redirects,stored,state,classes};
}
function reply(request,data,status=200){request.resolve({ok:status<400,status,json:async()=>data});}
(async()=>{
 let f=fixture();assert.equal(f.requests[0].url,'/auth/me');
 reply(f.requests.shift(),{user:{id:'alice'},csrf_token:'csrf',kb_id:'kb'});await tick();
 assert.equal(f.classes.has('auth-pending'),false);assert.equal(f.stored.size,0,'凭证不写入 storage');
 const call=f.context.api('/upload',{method:'POST'});await tick();
 assert.equal(f.requests[0].options.headers.get('X-CSRF-Token'),'csrf');
 reply(f.requests.shift(),{ok:true});await call;
 const late=f.context.api('/history/private');await tick();const request=f.requests.shift();
 f.events.storage({key:'zhiku.auth-change'});reply(request,{items:['old secret']});
 await assert.rejects(late,/账号已切换/);
 assert.deepEqual(f.redirects,['/login.html']);assert.equal(f.state.stream.closed,true);assert.equal(f.elements.get('dialog').closed,true);
 assert.equal(f.elements.get('#messages').cleared,true);
 f=fixture();reply(f.requests.shift(),{user:{id:'bob'},csrf_token:'b',kb_id:'b'});await tick();
 assert.equal(f.classes.has('auth-pending'),true);assert.deepEqual(f.redirects,['/login.html']);
 f=fixture();reply(f.requests.shift(),{user:{id:'alice'},csrf_token:'a',kb_id:'a'});await tick();
 const expired=f.context.api('/tasks');await tick();reply(f.requests.shift(),{detail:'expired'},401);
 await assert.rejects(expired,/重新登录/);assert.equal(f.elements.get('dialog').closed,true);
 console.log('认证页面检查通过：CSRF、身份匹配、401 清理、多标签切换、旧响应隔离。');
})().catch(error=>{console.error(error);process.exitCode=1;});
