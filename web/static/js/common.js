/* 公共交互：同源接口、侧栏会话、删除确认及移动导航。 */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const labels = {upload_file:'上传文件',store_file:'原文件备份',node_entry:'文件识别',node_pdf_to_md:'PDF 解析',node_md_img:'图片处理',node_document_split:'文档切片',node_item_name_recognition:'主体识别',node_bge_embedding:'向量化',node_import_milvus:'存入知识库',node_item_name_confirm:'确认产品',node_search_embedding:'知识库检索',node_search_embedding_hyde:'假设文档检索',node_web_search_mcp:'网络搜索',node_rrf:'结果融合',node_rerank:'相关性排序',node_answer_output:'生成回答'};
const statusNames = {pending:'排队中',processing:'处理中',completed:'已完成',failed:'失败'};
const state = {view:document.body.dataset.view,session:localStorage.getItem('zhiku.session') || crypto.randomUUID(),sessions:[],busy:false,sessionsVersion:0,deleteTarget:null,deleting:false,toastTimer:null};
const icons = () => window.lucide?.createIcons();
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const icon = (name) => `<i data-lucide="${name}"></i>`;
function toast(message) {
  $('#toast').textContent = message;$('#toast').hidden = false;
  clearTimeout(state.toastTimer);state.toastTimer = setTimeout(() => $('#toast').hidden = true, 5000);
}
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入后重试。');
  return data;
}
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
  localStorage.setItem('zhiku.session',sessionId);
  if(state.view==='chat') {
    closeMenu();
    window.dispatchEvent(new CustomEvent('zhiku:open-session',{detail:sessionId}));
  } else location.assign('/chat.html');
}
function newChat() {
  state.session=crypto.randomUUID();localStorage.setItem('zhiku.session',state.session);
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
const sessionsReady=loadSessions();
// 页面离开时停止定时器；浏览器从往返缓存恢复时重新同步公共状态。
window.addEventListener('pagehide',()=>{clearTimeout(state.toastTimer);});
window.addEventListener('pageshow',event=>{
  if(!event.persisted)return;
  state.session=localStorage.getItem('zhiku.session') || state.session;
  closeMenu();loadSessions();
});
