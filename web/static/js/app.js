/* 浏览器统一通过同源 FastAPI 服务访问后端接口。 */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const labels = {upload_file:'上传文件',store_file:'原文件备份',node_entry:'文件识别',node_pdf_to_md:'PDF 解析',node_md_img:'图片处理',node_document_split:'文档切片',node_item_name_recognition:'主体识别',node_bge_embedding:'向量化',node_import_milvus:'存入知识库',node_item_name_confirm:'确认产品',node_search_embedding:'知识库检索',node_search_embedding_hyde:'假设文档检索',node_web_search_mcp:'网络搜索',node_rrf:'结果融合',node_rerank:'相关性排序',node_answer_output:'生成回答'};
const statusNames = {pending:'排队中',processing:'处理中',completed:'已完成',failed:'失败'};
const state = {view:location.pathname === '/import.html' ? 'import':'chat',session:localStorage.getItem('zhiku.session') || crypto.randomUUID(),sessions:[],tasks:[],files:[],filter:'all',busy:false,uploading:false,stream:null,taskId:null,loadVersion:0,sessionsVersion:0,deleteTarget:null,deleting:false,taskTimer:null,pollTimer:null,toastTimer:null};
const icons = () => window.lucide?.createIcons();
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const icon = (name) => `<i data-lucide="${name}"></i>`;
function safeURL(value) {
  try {const url = new URL(value, location.origin);return ['http:','https:'].includes(url.protocol) ? url.href : null;} catch {return null;}
}
function markdown(value) {
  if (!window.marked || !window.DOMPurify) return escapeHTML(value).replace(/\n/g,'<br>');
  return DOMPurify.sanitize(marked.parse(value || '', {breaks:true}), {FORBID_TAGS:['style','form','input','button','iframe'],FORBID_ATTR:['style']});
}
function streamText(value) {
  return value.split(/(```[\s\S]*?```|`[^`\n]+`)/).map((part,i)=>i%2?part:part
    .split('【图片】')[0]
    .replace(/^\s*根据(?:所提供的)?参考内容[，,：:]\s*/, '')
    .replace(/[（(]\s*参考(?:内容|资料)?\s*(?:\[\d+\]\s*)+[）)]/g,'')
    .replace(/\[cite:\d+\]/g,'')
    .replace(/!\[[^\]]*\]\(https?:\/\/[^)]+\)/g,'')
    .replace(/(?:\[(?:c(?:i(?:t(?:e(?::\d*)?)?)?)?)?|【(?:图(?:片)?)?|[（(]参考[^）)]*|!\[[^\n]*)$/g,'')
  ).join('');
}
function sourceTitle(source) {return String(source.title || '知识库片段').replace(/^\s*#{1,6}\s*/, '').trim() || '知识库片段';}
function sourceExcerpt(source) {return String(source.content || '').replace(/!\[[^\]]*\]\([^)]+\)/g,'').replace(/\n{3,}/g,'\n\n').trim();}
function renderAnswer(article, sources = []) {
  article.sources=sources;
  const body=$('.message-body',article);body.innerHTML=markdown(article.answer);
  const byId=new Map(sources.map((source,index)=>[String(source.source_id),{source,number:index+1}]));
  const walker=document.createTreeWalker(body,NodeFilter.SHOW_TEXT);const nodes=[];
  while(walker.nextNode())if(!walker.currentNode.parentElement.closest('pre,code,a'))nodes.push(walker.currentNode);
  for(const node of nodes) {
    const matches=[...node.textContent.matchAll(/\[cite:(\d+)\]/g)];if(!matches.length)continue;
    const fragment=document.createDocumentFragment();let offset=0;
    for(const match of matches) {
      fragment.append(document.createTextNode(node.textContent.slice(offset,match.index)));
      const item=byId.get(match[1]);
      if(item){const button=document.createElement('button');button.type='button';button.className='citation';button.dataset.sourceId=match[1];button.textContent=`[${item.number}]`;button.title=sourceTitle(item.source);button.setAttribute('aria-label',`查看依据 ${item.number}：${sourceTitle(item.source)}`);fragment.append(button);}
      offset=match.index+match[0].length;
    }
    fragment.append(document.createTextNode(node.textContent.slice(offset)));node.replaceWith(fragment);
  }
  article.copyText=article.answer.replace(/\[cite:(\d+)\]/g,(_,id)=>byId.has(id)?`[${byId.get(id).number}]`:'');
}
function openSource(article, sourceId) {
  const source=article.sources?.find(item=>String(item.source_id)===sourceId);if(!source)return;
  $('#source-title').textContent=sourceTitle(source);
  $('#source-kind').textContent=source.source==='web'?'网络资料':'知识库资料';
  $('#source-excerpt').innerHTML=markdown(sourceExcerpt(source) || '暂无摘录');
  $$('a',$('#source-excerpt')).forEach(a=>{a.target='_blank';a.rel='noopener noreferrer';});
  const link=$('#source-link');const url=source.url?safeURL(source.url):null;
  link.hidden=!url;if(url)link.href=url;else link.removeAttribute('href');
  $('#source-dialog').showModal();$('#source-excerpt').scrollTop=0;
}
function toast(message) {
  $('#toast').textContent = message;$('#toast').hidden = false;
  clearTimeout(state.toastTimer);state.toastTimer = setTimeout(() => $('#toast').hidden = true, 5000);
}
async function copyAnswer(text) {
  try {
    if (!navigator.clipboard) throw new Error('Clipboard unavailable');
    await Promise.race([navigator.clipboard.writeText(text),new Promise((_,reject)=>setTimeout(()=>reject(new Error('Clipboard timeout')),1500))]);
    toast('答案已复制。');
  } catch {
    const input=document.createElement('textarea');input.value=text;input.className='copy-buffer';document.body.append(input);input.select();
    const copied=document.execCommand('copy');input.remove();toast(copied?'答案已复制。':'复制失败，请手动选择答案。');
  }
}
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入后重试。');
  return data;
}
function setBusy(busy, text) {
  state.busy = busy;
  $('#send').disabled = busy;$('#stream-mode').disabled = busy;$('#clear-chat').disabled = busy;
  $$('[data-delete-session]').forEach(button=>{button.disabled=state.deleting || (busy && button.dataset.deleteSession===state.session);});
  $('#composer-status').innerHTML = `<span class="status-dot ${busy?'amber':'good'}"></span>${escapeHTML(text || (busy?'正在处理':'等待提问'))}`;
}
function disconnect() {
  state.stream?.close();state.stream = null;clearTimeout(state.pollTimer);state.taskId = null;setBusy(false);
}
function scrollBottom(force = false) {
  const area = $('#messages');
  if (force || area.scrollHeight - area.scrollTop - area.clientHeight < 180) area.scrollTop = area.scrollHeight;
}
function updateCount() {$('#conversation-count').textContent = `${$$('.message').length} 条消息`;}
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
  $('#delete-session-title').textContent=state.sessions.find(item=>item.session_id===sessionId)?.title || $('#chat-title').textContent || '新会话';
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
function setView(view, push = true) {
  state.view = view;$('#chat-view').hidden = view !== 'chat';$('#import-view').hidden = view !== 'import';
  $('#view-name').textContent = view === 'chat'?'知识问答':'文档管理';
  document.title = `掌柜智库 · ${$('#view-name').textContent}`;
  $$('[data-view]').forEach(a => a.classList.toggle('active', a.dataset.view === view));
  if (push) history.pushState({},'',view === 'chat'?'/chat.html':'/import.html');
  closeMenu();
  if (view === 'import') loadTasks();
}
function syncSidebar() {$('#sidebar').inert=matchMedia('(max-width:700px)').matches && !$('#sidebar').classList.contains('open');}
function closeMenu() {$('#sidebar').classList.remove('open');$('#backdrop').hidden = true;syncSidebar();}
function newChat() {
  disconnect();state.loadVersion++;state.session=crypto.randomUUID();localStorage.setItem('zhiku.session',state.session);
  $('#messages').replaceChildren(emptyState());$('#chat-title').textContent='新会话';$('#question').value='';updateInput();updateCount();renderSessions();setView('chat');$('#question').focus();
}
function emptyState() {
  const node=document.createElement('div');node.className='chat-empty';node.innerHTML='<img src="/static/assets/brand.png" alt="" width="64" height="64"><h2>掌柜智库</h2><p>知识问答</p><div class="empty-rule"></div>';return node;
}
function addMessage(role, text = '', pending = false) {
  $('.chat-empty')?.remove();
  $('#messages > .task-empty')?.remove();
  const article=document.createElement('article');article.className=`message ${role}`;
  article.classList.toggle('streaming',pending);
  const head=document.createElement('div');head.className='message-head';head.innerHTML=role==='assistant'?'<img src="/static/assets/brand.png" alt="">掌柜智库':'你';
  const body=document.createElement('div');body.className='message-body';
  if (role==='user') body.textContent=text;
  else if (pending) body.innerHTML='<span class="thinking">正在准备</span>';
  else body.innerHTML=markdown(text);
  article.append(head,body);$('#messages').append(article);article.answer=text;article.query='';
  updateCount();return article;
}
function renderProgress(article, task) {
  let detail=$('.progress-detail',article);
  if (!detail) {detail=document.createElement('details');detail.className='progress-detail';article.insertBefore(detail,$('.message-body',article));}
  const running=task.running_list || [];const done=task.done_list || [];
  const title=task.status==='failed'?'处理失败':task.status==='completed'?'处理完成':running.length?running.map(n=>labels[n]||n).join(' · '):'排队中';
  detail.innerHTML=`<summary>${icon(task.status==='failed'?'circle-alert':task.status==='completed'?'circle-check':'loader-circle')}<span>${escapeHTML(title)}</span>${icon('chevron-down')}</summary><div class="progress-steps">${done.map(n=>`<span class="progress-step">${icon('check')}${escapeHTML(labels[n]||n)}</span>`).join('')}${running.map(n=>`<span class="progress-step">${icon('loader-circle')}${escapeHTML(labels[n]||n)}</span>`).join('')}</div>`;
  if (!article.answer && !['completed','failed'].includes(task.status)) $('.message-body',article).innerHTML=`<span class="thinking">${escapeHTML(title)}</span>`;
  icons();
}
function renderAttachments(article, data) {
  renderAnswer(article,data.sources || []);
  $('.source-details',article)?.remove();$('.image-list',article)?.remove();$('.warning-line',article)?.remove();
  if (data.sources?.length) {
    const details=document.createElement('details');details.className='source-details';
    details.innerHTML=`<summary>查看依据 · ${data.sources.length} 条</summary><div class="source-list">${data.sources.map((source,i)=>`<button type="button" class="source-item" data-source-id="${escapeHTML(source.source_id)}">${icon(source.source==='web'?'globe':'file-text')}<span><strong>${i+1}. ${escapeHTML(sourceTitle(source))}</strong><small>${source.source==='web'?'网络资料':'知识库资料'}</small><span class="source-preview">${escapeHTML(sourceExcerpt(source).slice(0,160))}</span></span>${icon('chevron-right')}</button>`).join('')}</div>`;article.append(details);
  }
  const urls=[...new Set((data.image_urls || []).map(safeURL).filter(Boolean))].slice(0,3);
  if (urls.length) {const images=document.createElement('div');images.className='image-list';urls.forEach((url,i)=>{const figure=document.createElement('figure');const image=document.createElement('img');image.alt=`资料图片 ${i+1}`;image.loading='lazy';image.referrerPolicy='no-referrer';image.onload=()=>{if(Math.min(image.naturalWidth,image.naturalHeight)<80 || Math.max(image.naturalWidth,image.naturalHeight)<160)figure.remove();};image.onerror=()=>figure.remove();image.src=url;const caption=document.createElement('figcaption');caption.textContent=image.alt;figure.append(image,caption);images.append(figure);});article.append(images);}
  if (data.warnings?.length) {const warning=document.createElement('p');warning.className='warning-line';warning.textContent=data.warnings.join(' ');article.append(warning);}
  $$('a',article).forEach(a=>{a.target='_blank';a.rel='noopener noreferrer';});
}
function footer(article, failed = false) {
  $('.message-footer',article)?.remove();const foot=document.createElement('div');foot.className='message-footer';
  foot.innerHTML=failed?`<button class="text-button retry-answer">${icon('rotate-ccw')}重试</button>`:`<button class="icon-button copy-answer" title="复制答案" aria-label="复制答案">${icon('copy')}</button>`;
  article.append(foot);icons();
}
function finish(article, data) {
  article.classList.remove('streaming');
  article.answer=data.answer || '';
  renderProgress(article,{...data,status:'completed'});renderAttachments(article,data);footer(article);
  disconnect();loadSessions();scrollBottom();
}
function fail(article, message) {
  article.classList.remove('streaming');
  article.classList.add('error');$('.message-body',article).textContent=article.answer?`${streamText(article.answer)}\n\n${message}`:message;
  renderProgress(article,{status:'failed'});footer(article,true);disconnect();loadSessions();scrollBottom();
}
async function pollTask(article, taskId) {
  if (state.taskId!==taskId) return;
  try {
    const task=await api(`/status/${encodeURIComponent(taskId)}`);
    if (state.taskId!==taskId) return;
    if (task.status==='completed') {finish(article,{...task.result,...task,result:undefined});return;}
    if (task.status==='failed') {fail(article,task.error);return;}
    renderProgress(article,task);
  } catch(e) {
    if (state.taskId!==taskId) return;
    if (/不存在|过期/.test(e.message)) {fail(article,e.message);return;}
    $('#composer-status').textContent='连接中断，正在重连…';
  }
  state.pollTimer=setTimeout(()=>pollTask(article,taskId),3000);
}
function connectStream(article, taskId) {
  state.taskId=taskId;setBusy(true);
  const source=new EventSource(`/stream/${encodeURIComponent(state.session)}?task_id=${encodeURIComponent(taskId)}`);state.stream=source;
  const valid=()=>state.stream===source && state.taskId===taskId;
  source.addEventListener('progress',e=>{if(valid())renderProgress(article,JSON.parse(e.data));});
  source.addEventListener('delta',e=>{if(!valid())return;const data=JSON.parse(e.data);article.answer+=(data.delta ?? data.text ?? '');$('.message-body',article).textContent=streamText(article.answer);scrollBottom();});
  source.addEventListener('final',e=>{if(valid())finish(article,JSON.parse(e.data));});
  source.addEventListener('error',e=>{if(!valid())return;if(e.data)fail(article,JSON.parse(e.data).error || '处理失败。');else $('#composer-status').textContent='连接中断，正在重连…';});
  state.pollTimer=setTimeout(()=>pollTask(article,taskId),3000);
}
async function loadHistory(sessionId) {
  disconnect();state.session=sessionId;localStorage.setItem('zhiku.session',sessionId);renderSessions();
  const version=++state.loadVersion;setBusy(true,'正在加载会话');$('#messages').innerHTML='<div class="task-empty">正在加载会话…</div>';
  $('#chat-title').textContent=state.sessions.find(s=>s.session_id===sessionId)?.title || '新会话';
  try {
    const data=await api(`/history/${encodeURIComponent(sessionId)}`);
    if(version!==state.loadVersion)return;
    $('#messages').replaceChildren();
    setBusy(false);
    for(const item of data.items){const article=addMessage(item.role,item.text);if(item.role==='assistant'){renderAttachments(article,item);footer(article);}}
    const task=data.active_task;
    if(task && !['completed','failed'].includes(task.status)) {
      if(!data.items.length || data.items.at(-1).role!=='user') addMessage('user',task.query);
      const article=addMessage('assistant','',true);article.query=task.query;connectStream(article,task.task_id);
    }
    if(!$('.message'))$('#messages').append(emptyState());
    updateCount();scrollBottom(true);
  } catch(e) {
    if(version!==state.loadVersion)return;
    setBusy(false);
    $('#messages').innerHTML=`<div class="task-empty"><span>${escapeHTML(e.message)}</span><button class="text-button" id="retry-history">${icon('refresh-cw')}重新加载</button></div>`;icons();$('#retry-history').onclick=()=>loadHistory(sessionId);
  }
}
function updateInput() {const q=$('#question');$('#char-count').textContent=`${q.value.length} / 4000`;q.style.height='auto';q.style.height=`${Math.min(q.scrollHeight,160)}px`;}
async function sendQuestion(question) {
  if(state.busy || !question.trim())return;
  const session=state.session;const version=++state.loadVersion;
  addMessage('user',question);const answer=addMessage('assistant','',true);answer.query=question;
  $('#question').value='';updateInput();setBusy(true,'正在提交');scrollBottom(true);
  if($('#chat-title').textContent==='新会话')$('#chat-title').textContent=question;
  try {
    const data=await api('/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:question,session_id:session,is_stream:$('#stream-mode').checked})});
    if(version!==state.loadVersion || session!==state.session)return;
    if('answer' in data)finish(answer,data);else connectStream(answer,data.task_id);
    loadSessions();
  } catch(e) {if(version===state.loadVersion && session===state.session)fail(answer,e.message);}
}
function queueFiles(files) {
  if(state.uploading)return;
  for(const file of files) {
    if(!/\.(pdf|md)$/i.test(file.name)){toast(`不支持的文件：${file.name}`);continue;}
    if(!file.size || file.size>50*1024*1024){toast(`${file.name} 为空或超过 50 MB。`);continue;}
    if(state.files.length>=10){toast('每次最多选择 10 个文件。');break;}
    if(!state.files.some(f=>f.name===file.name && f.size===file.size))state.files.push(file);
  }
  renderQueue();
}
function renderQueue() {
  $('#upload-queue').innerHTML=state.files.map((f,i)=>`<div class="queue-row">${icon('file-text')}<span>${escapeHTML(f.name)}</span><small>${f.size<1024*1024?`${Math.ceil(f.size/1024)} KB`:`${(f.size/1024/1024).toFixed(1)} MB`}</small><button class="icon-button" data-remove="${i}" title="移除文件" aria-label="移除 ${escapeHTML(f.name)}" ${state.uploading?'disabled':''}>${icon('x')}</button></div>`).join('');
  $('#upload-actions').hidden=!state.files.length;$('#selection-count').textContent=`已选择 ${state.files.length} 份文档`;$('#upload-button').disabled=state.uploading;$('#choose-files').disabled=state.uploading;icons();
}
function uploadFiles() {
  if(state.uploading || !state.files.length)return;
  state.uploading=true;renderQueue();$('#upload-meter').hidden=false;$('#upload-progress').value=0;$('#upload-percent').textContent='0%';
  const form=new FormData();state.files.forEach(f=>form.append('files',f));
  const xhr=new XMLHttpRequest();xhr.open('POST','/upload');xhr.timeout=180000;
  xhr.upload.onprogress=e=>{if(e.lengthComputable){const p=Math.round(e.loaded/e.total*100);$('#upload-progress').value=p;$('#upload-percent').textContent=p===100?'正在接收…':`${p}%`;}};
  xhr.onload=()=>{let data;try{data=JSON.parse(xhr.responseText);}catch{data={detail:'上传失败。'};}
    if(xhr.status>=200 && xhr.status<300){toast(`${data.task_ids.length} 份文档已进入导入队列。`);state.files=[];loadTasks();}
    else toast(typeof data.detail==='string'?data.detail:'上传失败，请重试。');
  };
  xhr.onerror=()=>toast('上传连接失败，请检查服务状态。');xhr.ontimeout=()=>toast('上传超时，请重试。');
  xhr.onloadend=()=>{state.uploading=false;$('#upload-meter').hidden=true;$('#files').value='';renderQueue();};xhr.send(form);
}
function renderTasks() {
  $('#stat-total').innerHTML=`${state.tasks.length}<small>份</small>`;$('#stat-done').innerHTML=`${state.tasks.filter(t=>t.status==='completed').length}<small>份</small>`;$('#stat-running').innerHTML=`${state.tasks.filter(t=>['pending','processing'].includes(t.status)).length}<small>份</small>`;
  const filter=$('#task-search').value.toLowerCase();
  const tasks=state.tasks.filter(t=>(state.filter==='all' || (state.filter==='processing'?['pending','processing'].includes(t.status):t.status===state.filter)) && t.filename?.toLowerCase().includes(filter));
  const open=new Set($$('.task-item[open]').map(x=>x.dataset.task));
  $('#task-list').innerHTML=tasks.length?tasks.map(task=>`<details class="task-item" data-task="${task.task_id}" ${open.has(task.task_id)?'open':''}><summary class="task-summary"><div class="file-name"><span class="file-icon ${/\.pdf$/i.test(task.filename)?'pdf':''}">${icon('file-text')}</span><div><strong title="${escapeHTML(task.filename)}">${escapeHTML(task.filename)}</strong><small>${task.result.chunk_count!==undefined?`${task.result.chunk_count} 个切片`:(task.running_list.map(n=>labels[n]||n).join(' · ') || statusNames[task.status])}</small></div></div><span class="status-label ${task.status}"><span class="status-dot ${task.status==='completed'?'good':task.status==='failed'?'bad':'amber'}"></span>${statusNames[task.status]}</span><span class="task-time">${new Date(task.created_at*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</span>${icon('chevron-down')}</summary><div class="task-detail"><div class="progress-steps">${task.done_list.map(n=>`<span class="progress-step">${icon('check')}${escapeHTML(labels[n]||n)}</span>`).join('')}${task.running_list.map(n=>`<span class="progress-step">${icon('loader-circle')}${escapeHTML(labels[n]||n)}</span>`).join('')}</div>${task.error?`<p class="warning-line">${escapeHTML(task.error)}</p><button class="text-button retry-import" data-filename="${escapeHTML(task.filename)}">${icon('upload')}重新选择文件</button>`:''}${task.warnings.map(w=>`<p class="warning-line">${escapeHTML(w)}</p>`).join('')}${task.result.item_name?`<p class="muted small" style="margin-top:12px">${escapeHTML(task.result.item_name)}</p>`:''}</div></details>`).join(''):`<div class="task-empty">${icon('files')}<span>${state.tasks.length?'暂无匹配的文档':'暂无导入记录'}</span></div>`;
  icons();
}
async function loadTasks() {
  clearTimeout(state.taskTimer);
  try {state.tasks=(await api('/tasks')).items;renderTasks();}
  catch(e){$('#task-list').innerHTML=`<div class="task-empty">${escapeHTML(e.message)}</div>`;}
  if(state.view==='import')state.taskTimer=setTimeout(loadTasks,2500);
}
async function refreshHealth() {
  $('#refresh-health').disabled=true;
  try {const result=await api('/health/services');$('#health-dot').className=`status-dot ${result.ok?'good':'amber'}`;$('#health-label').textContent=result.ok?'服务已连接':'部分服务离线';$('#service-list').innerHTML=Object.entries(result.services).map(([name,status])=>`<div class="service-row"><span>${name}</span><span><span class="status-dot ${status==='reachable'?'good':'bad'}"></span> ${status==='reachable'?'可连接':'未连接'}</span></div>`).join('');}
  catch {$('#health-dot').className='status-dot bad';$('#health-label').textContent='服务未连接';$('#service-list').textContent='无法连接应用服务。';}
  finally{$('#refresh-health').disabled=false;}
}
$('#query-form').onsubmit=e=>{e.preventDefault();sendQuestion($('#question').value.trim());};
$('#question').oninput=updateInput;
$('#question').onkeydown=e=>{if(e.key==='Enter' && !e.shiftKey && !e.isComposing){e.preventDefault();$('#query-form').requestSubmit();}};
$('#new-chat').onclick=newChat;
$('#menu-button').onclick=()=>{$('#sidebar').classList.add('open');$('#backdrop').hidden=false;syncSidebar();};$('#backdrop').onclick=closeMenu;
matchMedia('(max-width:700px)').addEventListener('change',syncSidebar);
$$('[data-view]').forEach(a=>a.onclick=e=>{e.preventDefault();setView(a.dataset.view);});
window.onpopstate=()=>setView(location.pathname==='/import.html'?'import':'chat',false);
$('#session-list').onclick=e=>{const remove=e.target.closest('[data-delete-session]');if(remove){requestDeleteSession(remove.dataset.deleteSession);return;}const button=e.target.closest('[data-session]');if(button){setView('chat');loadHistory(button.dataset.session);}};
$('#clear-chat').onclick=()=>requestDeleteSession(state.session);
$('#delete-session-form').onsubmit=deleteSession;
$('#confirm-dialog').oncancel=e=>{if(state.deleting)e.preventDefault();};
$('#confirm-dialog').onclose=()=>{state.deleteTarget=null;};
$('#messages').onclick=async e=>{
  const article=e.target.closest('.message');
  if(e.target.closest('.copy-answer'))await copyAnswer(article.copyText ?? streamText(article.answer));
  const citation=e.target.closest('[data-source-id]');if(citation)openSource(article,citation.dataset.sourceId);
  if(e.target.closest('.retry-answer')){if(!state.busy)sendQuestion(article.query);}
  if(e.target.tagName==='IMG' && !e.target.closest('.message-head')){const src=safeURL(e.target.src);if(src){$('#preview-image').src=src;$('#image-dialog').showModal();}}
};
$('#close-image').onclick=()=>$('#image-dialog').close();
$('#close-source').onclick=()=>$('#source-dialog').close();
$('#choose-files').onclick=()=>$('#files').click();$('#files').onchange=e=>queueFiles(e.target.files);
$('#upload-queue').onclick=e=>{const button=e.target.closest('[data-remove]');if(button && !state.uploading){state.files.splice(Number(button.dataset.remove),1);renderQueue();}};
$('#dropzone').ondragover=e=>{e.preventDefault();if(!state.uploading)$('#dropzone').classList.add('dragging');};
$('#dropzone').ondragleave=()=>$('#dropzone').classList.remove('dragging');
$('#dropzone').ondrop=e=>{e.preventDefault();$('#dropzone').classList.remove('dragging');queueFiles(e.dataTransfer.files);};
$('#upload-button').onclick=uploadFiles;$('#refresh-tasks').onclick=loadTasks;$('#refresh-health').onclick=refreshHealth;
$('#task-search').oninput=renderTasks;
$$('[data-filter]').forEach(b=>b.onclick=()=>{state.filter=b.dataset.filter;$$('[data-filter]').forEach(x=>x.setAttribute('aria-selected',String(x===b)));renderTasks();});
$('#task-list').onclick=e=>{if(e.target.closest('.retry-import'))$('#files').click();};
document.addEventListener('error',e=>{if(e.target.tagName==='IMG' && e.target.closest('.message')){e.target.alt='图片暂不可用';e.target.title='图片加载失败';}},true);
setView(state.view,false);icons();refreshHealth();setInterval(refreshHealth,60000);
loadSessions().then(()=>loadHistory(state.session));
