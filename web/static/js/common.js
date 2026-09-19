/* 公共交互：同源接口、侧栏会话、删除确认及移动导航。 */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const labels = {upload_file:'上传文件',store_file:'原文件备份',node_entry:'文件识别',node_pdf_to_md:'PDF 解析',node_md_img:'图片处理',node_document_split:'文档切片',node_item_name_recognition:'主体识别',node_bge_embedding:'向量化',node_import_milvus:'存入知识库',node_item_name_confirm:'确认产品',node_search_embedding:'知识库检索',node_search_embedding_hyde:'假设文档检索',node_web_search_mcp:'网络搜索',node_rrf:'结果融合',node_rerank:'相关性排序',node_answer_output:'生成回答'};
const statusNames = {pending:'排队中',processing:'处理中',completed:'已完成',failed:'失败'};
const state = {view:document.body.dataset.view,session:null,sessions:[],busy:false,sessionsVersion:0,deleteTarget:null,deleting:false,toastTimer:null};
const icons = () => window.lucide?.createIcons();
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const icon = (name) => `<i data-lucide="${name}"></i>`;
function toast(message) {
  $('#toast').textContent = message;$('#toast').hidden = false;
  clearTimeout(state.toastTimer);state.toastTimer = setTimeout(() => $('#toast').hidden = true, 5000);
}
// 会话选择按用户 ID 分开保存，切换账号时不会沿用另一账号的会话 ID。
const accountKey=`zhiku.session.${document.body.dataset.userId}`;
state.session=localStorage.getItem(accountKey);
const authState={csrf:null,kbId:null,epoch:0,expired:false};
function expireAuth(broadcast=false){
  // 1. 递增账号版本并停止流、轮询和上传，让尚未返回的旧请求结果失效。
  if(authState.expired)return;
  authState.expired=true;authState.epoch++;state.sessionsVersion++;state.loadVersion++;
  authState.csrf=null;state.stream?.close();clearTimeout(state.pollTimer);state.uploadXHR?.abort();
  window.dispatchEvent(new Event('zhiku:auth-expired'));
  // 2. 清空私有内容和弹窗，防止跳转或浏览器返回期间闪现旧账号数据。
  $$('dialog').forEach(dialog=>dialog.close());
  $('#messages')?.replaceChildren();$('#session-list')?.replaceChildren();$('#task-list')?.replaceChildren();$('#document-list')?.replaceChildren();
  $('#preview-image')?.removeAttribute('src');$('#source-excerpt')?.replaceChildren();
  document.body.classList.add('auth-pending');
  // 3. 主动退出或改密时通知其他标签页，然后回到登录页。
  if(broadcast)localStorage.setItem('zhiku.auth-change',crypto.randomUUID());
  location.replace('/login.html');
}
async function verifyIdentity(){
  // 页面恢复或重新获得焦点时复查身份，用户和 CSRF 令牌须与当前页面一致。
  document.body.classList.add('auth-pending');
  try{
    const response=await fetch('/auth/me',{cache:'no-store'});
    if(!response.ok){expireAuth();return false;}
    const data=await response.json();
    if(authState.expired || data.user.id!==document.body.dataset.userId || (authState.csrf && authState.csrf!==data.csrf_token)){expireAuth();return false;}
    authState.csrf=data.csrf_token;authState.kbId=data.kb_id;
    document.body.classList.remove('auth-pending');return true;
  }catch{expireAuth();return false;}
}
const authReady=verifyIdentity();
async function api(path, options = {}) {
  // 统一等待身份校验，写请求附加 CSRF 令牌，并丢弃账号切换前发出的响应。
  if(!await authReady || authState.expired)throw new Error('请重新登录。');
  const epoch=authState.epoch;
  const headers=new Headers(options.headers || {});
  if(!['GET','HEAD'].includes((options.method || 'GET').toUpperCase()))headers.set('X-CSRF-Token',authState.csrf);
  const response = await fetch(path, {...options,headers});
  const data = await response.json().catch(() => ({}));
  if(epoch!==authState.epoch || authState.expired)throw new Error('账号已切换。');
  if(response.status===401){expireAuth();throw new Error('请重新登录。');}
  if (!response.ok){const error=new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入后重试。');error.status=response.status;throw error;}
  return data;
}
// 跨标签页账号变化和窗口重新获得焦点时，都重新确认当前页面仍属于该用户。
window.addEventListener('storage',event=>{if(event.key==='zhiku.auth-change')expireAuth();});
window.addEventListener('focus',()=>{if(!authState.expired)verifyIdentity();});
$('#logout').onclick=async()=>{try{await api('/auth/logout',{method:'POST'});expireAuth(true);}catch(e){toast(e.message);}};
$('#change-password').onclick=()=>{$('#password-form').reset();$('#password-error').hidden=true;$('#password-dialog').showModal();};
$('#close-password').onclick=()=>$('#password-dialog').close();
$('#password-form').onsubmit=async event=>{
  // 修改成功后旧登录全部失效，当前页面也清除私有内容并跳转登录。
  event.preventDefault();const button=event.submitter;button.disabled=true;
  try{await api('/auth/change-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(event.target)))});expireAuth(true);}
  catch(e){$('#password-error').textContent=e.message;$('#password-error').hidden=false;}
  finally{button.disabled=false;}
};
function renderSessions() {
  $('#session-list').innerHTML = state.sessions.length ? state.sessions.map(item => `<div class="session-row ${item.session_id===state.session?'active':''}"><button class="session-button" data-session="${escapeHTML(item.session_id)}" title="${escapeHTML(item.title || '新会话')}">${icon('message-square')}<span>${escapeHTML(item.title || '新会话')}</span></button><button type="button" class="icon-button session-delete" data-delete-session="${escapeHTML(item.session_id)}" title="删除会话" aria-label="删除会话：${escapeHTML(item.title || '新会话')}" ${state.deleting || (state.busy && item.session_id===state.session)?'disabled':''}>${icon('trash-2')}</button></div>`).join('') : '<p class="muted small">暂无会话</p>';
  icons();
}
async function loadSessions() {
  const version=++state.sessionsVersion;
  try {const data=await api('/sessions');if(version!==state.sessionsVersion)return;state.sessions=data.items;renderSessions();}
  catch (e) {if(version!==state.sessionsVersion)return;$('#session-list').innerHTML = `<p class="muted small">${escapeHTML(e.message)}</p><button class="text-button" id="retry-sessions">${icon('refresh-cw')}重新加载</button>`;icons();$('#retry-sessions').onclick=loadSessions;}
}
function requestDeleteSession(sessionId) {
  if(state.deleting)return;
  if(state.busy && sessionId===state.session){toast('当前会话仍在处理中，暂时无法删除。');return;}
  state.deleteTarget=sessionId;
  $('#delete-session-title').textContent=state.sessions.find(item=>item.session_id===sessionId)?.title || $('#chat-title')?.textContent || '新会话';
  $('#delete-error').hidden=true;$('#delete-error').textContent='';
  $('#confirm-dialog').returnValue='';$('#confirm-dialog').showModal();
}
async function deleteSession(event) {
  if(event.submitter?.value!=='confirm')return;
  event.preventDefault();
  const sessionId=state.deleteTarget;if(!sessionId || state.deleting)return;
  state.deleting=true;
  const dialog=$('#confirm-dialog');
  $$('button',dialog).forEach(button=>{button.disabled=true;});
  $('#confirm-delete').textContent='正在删除…';$('#delete-error').hidden=true;
  try {
    await api(`/history/${encodeURIComponent(sessionId)}`,{method:'DELETE'});
    state.sessionsVersion++;
    state.sessions=state.sessions.filter(item=>item.session_id!==sessionId);
    if(state.session===sessionId)newChat();else renderSessions();
    dialog.close();toast('会话已删除。');loadSessions();
  } catch(e) {
    $('#delete-error').textContent=e.message;$('#delete-error').hidden=false;
  } finally {
    state.deleting=false;
    $$('button',dialog).forEach(button=>{button.disabled=false;});
    $('#confirm-delete').textContent='删除会话';
    $$('[data-delete-session]').forEach(button=>{button.disabled=state.busy && button.dataset.deleteSession===state.session;});
  }
}
// 页面内通过事件通知聊天脚本；跨页面时使用真实地址跳转。
function openSession(sessionId) {
  localStorage.setItem(accountKey,sessionId);
  if(state.view==='chat') {
    closeMenu();
    window.dispatchEvent(new CustomEvent('zhiku:open-session',{detail:sessionId}));
  } else location.assign('/chat.html');
}
function newChat() {
  state.session=null;localStorage.removeItem(accountKey);
  if(state.view==='chat') {
    closeMenu();window.dispatchEvent(new Event('zhiku:new-chat'));renderSessions();
  } else location.assign('/chat.html');
}
function syncSidebar() {$('#sidebar').inert=matchMedia('(max-width:700px)').matches && !$('#sidebar').classList.contains('open');}
function closeMenu() {$('#sidebar').classList.remove('open');$('#backdrop').hidden = true;syncSidebar();}
// 公共控件只绑定一次，导入页不依赖任何聊天区域元素。
$('#new-chat').onclick=newChat;
$('#menu-button').onclick=()=>{$('#sidebar').classList.add('open');$('#backdrop').hidden=false;syncSidebar();};
$('#backdrop').onclick=closeMenu;
matchMedia('(max-width:700px)').addEventListener('change',syncSidebar);
$('#session-list').onclick=e=>{
  const remove=e.target.closest('[data-delete-session]');
  if(remove){requestDeleteSession(remove.dataset.deleteSession);return;}
  const button=e.target.closest('[data-session]');if(button)openSession(button.dataset.session);
};
$('#delete-session-form').onsubmit=deleteSession;
$('#confirm-dialog').oncancel=e=>{if(state.deleting)e.preventDefault();};
$('#confirm-dialog').onclose=()=>{state.deleteTarget=null;};
syncSidebar();icons();
const sessionsReady=authReady.then(valid=>{if(valid)return loadSessions();});
// 页面离开时停止定时器；浏览器从往返缓存恢复时重新同步公共状态。
window.addEventListener('pagehide',()=>{clearTimeout(state.toastTimer);});
window.addEventListener('pageshow',event=>{
  if(!event.persisted)return;
  verifyIdentity().then(valid=>{if(valid){state.session=localStorage.getItem(accountKey);closeMenu();loadSessions();window.dispatchEvent(new Event('zhiku:restore'));}});
});
