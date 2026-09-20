/* 聊天页：消息、流式订阅、历史恢复、引用与图片展示。公共能力由 common.js 提供。 */
Object.assign(state,{stream:null,taskId:null,loadVersion:0,pollTimer:null});
function safeURL(value) {
  try {const url = new URL(value, location.origin);return ['http:','https:'].includes(url.protocol) ? url.href : null;} catch {return null;}
}
// 图片仅允许本站受保护的资产地址；实际知识库权限仍由下载接口校验。
function safeImageURL(value) {try{const url=new URL(value,location.origin);return url.origin===location.origin && /^\/assets\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(url.pathname) && !url.search && !url.hash?url.href:null;}catch{return null;}}
function markdown(value, imageURLs = []) {
  if (!window.marked || !window.DOMPurify) return escapeHTML(value).replace(/\n/g,'<br>');
  const allowed=new Set(imageURLs.map(safeImageURL).filter(Boolean));
  const renderer=new marked.Renderer();
  // 在生成 HTML 前校验地址；禁止原始 HTML 绕过白名单触发图片或其他资源加载。
  renderer.html=({text})=>escapeHTML(text);
  renderer.image=({href,text})=>{
    const url=safeImageURL(href);
    return url && allowed.has(url)?`<img src="${escapeHTML(url)}" alt="${escapeHTML(text || '资料图片')}" loading="lazy" referrerpolicy="no-referrer">`:'';
  };
  return DOMPurify.sanitize(marked.parse(value || '', {breaks:true,renderer}), {USE_PROFILES:{html:true},FORBID_TAGS:['style','form','input','button','iframe'],FORBID_ATTR:['style','srcset']});
}
function streamText(value) {
  return value.split(/(```[\s\S]*?```|`[^`\n]+`)/).map((part,i)=>i%2?part:part
    .split('【图片】')[0]
    .replace(/^\s*根据(?:所提供的)?参考内容[，,：:]\s*/, '')
    .replace(/[（(]\s*参考(?:内容|资料)?\s*(?:\[\d+\]\s*)+[）)]/g,'')
    .replace(/\[cite:\d+\]/g,'')
    .replace(/!\[[^\]]*\]\([^)]+\)/g,'')
    .replace(/(?:\[(?:c(?:i(?:t(?:e(?::\d*)?)?)?)?)?|【(?:图(?:片)?)?|[（(]参考[^）)]*|!\[[\s\S]*|!)$/g,'')
  ).join('');
}
function sourceTitle(source) {return String(source.title || '知识库片段').replace(/^\s*#{1,6}\s*/, '').trim() || '知识库片段';}
function sourceExcerpt(source) {return String(source.content || '').replace(/!\[[^\]]*\]\([^)]+\)/g,'').replace(/\n{3,}/g,'\n\n').trim();}
function renderAnswer(article, sources = [], imageURLs = []) {
  article.sources=sources;
  const body=$('.message-body',article);body.innerHTML=markdown(article.answer,imageURLs);
  // 记录已在正文出现的图片，旧版附件只补充未出现的地址，避免重复展示。
  article.inlineImages=new Set(Array.from(body.querySelectorAll('img'),image=>image.src));
  body.querySelectorAll('img').forEach(image=>{
    image.onload=()=>{if(Math.min(image.naturalWidth,image.naturalHeight)<80 || Math.max(image.naturalWidth,image.naturalHeight)<160)image.remove();};
    image.onerror=()=>image.remove();
    if(image.complete && image.naturalWidth)image.onload();
  });
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
  renderAnswer(article,data.sources || [],data.image_urls || []);
  $('.source-details',article)?.remove();$('.image-list',article)?.remove();$('.warning-line',article)?.remove();
  if (data.sources?.length) {
    const details=document.createElement('details');details.className='source-details';
    details.innerHTML=`<summary>查看依据 · ${data.sources.length} 条</summary><div class="source-list">${data.sources.map((source,i)=>`<button type="button" class="source-item" data-source-id="${escapeHTML(source.source_id)}">${icon(source.source==='web'?'globe':'file-text')}<span><strong>${i+1}. ${escapeHTML(sourceTitle(source))}</strong><small>${source.source==='web'?'网络资料':'知识库资料'}</small><span class="source-preview">${escapeHTML(sourceExcerpt(source).slice(0,160))}</span></span>${icon('chevron-right')}</button>`).join('')}</div>`;article.append(details);
  }
  const urls=[...new Set((data.image_urls || []).map(safeImageURL).filter(url=>url && !article.inlineImages.has(url)))];
  if (urls.length) {
    const images=document.createElement('div');images.className='image-list';
    // 小图或加载失败的图片被移除后，按剩余图片重排编号，避免从“图片 2”开始。
    const renumber=()=>images.querySelectorAll('figure').forEach((figure,i)=>{
      figure.querySelector('img').alt=`资料图片 ${i+1}`;
      figure.querySelector('figcaption').textContent=`资料图片 ${i+1}`;
    });
    urls.forEach(url=>{
      const figure=document.createElement('figure');const image=document.createElement('img');
      image.loading='lazy';image.referrerPolicy='no-referrer';
      const remove=()=>{figure.remove();renumber();};
      image.onload=()=>{if(Math.min(image.naturalWidth,image.naturalHeight)<80 || Math.max(image.naturalWidth,image.naturalHeight)<160)remove();};
      image.onerror=remove;
      const caption=document.createElement('figcaption');
      figure.append(image,caption);images.append(figure);image.src=url;
    });
    renumber();article.append(images);
  }
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
  source.addEventListener('error',e=>{if(!valid())return;if(e.data){const data=JSON.parse(e.data);if(data.code==='AUTH_EXPIRED'){expireAuth();return;}fail(article,data.error || '处理失败。');}else $('#composer-status').textContent='连接中断，正在重连…';});
  state.pollTimer=setTimeout(()=>pollTask(article,taskId),3000);
}
async function loadHistory(sessionId) {
  if(!sessionId){resetChat();return;}
  disconnect();state.session=sessionId;localStorage.setItem(accountKey,sessionId);renderSessions();
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
    if(e.status===404){newChat();return;}
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
    // 知识库取自登录初始化结果；没有会话 ID 时由服务端创建会话并确定归属。
    const data=await api('/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:question,kb_id:authState.kbId,...(session?{session_id:session}:{}),is_stream:$('#stream-mode').checked})});
    if(version!==state.loadVersion || session!==state.session)return;
    state.session=data.session_id;localStorage.setItem(accountKey,state.session);
    if('answer' in data)finish(answer,data);else connectStream(answer,data.task_id);
    loadSessions();
  } catch(e) {if(version===state.loadVersion && session===state.session)fail(answer,e.message);}
}
// 新建会话只重置聊天页状态，后台已有任务不会因页面断开而被取消。
function resetChat() {
  disconnect();state.loadVersion++;
  $('#messages').replaceChildren(emptyState());$('#chat-title').textContent='新会话';
  $('#question').value='';updateInput();updateCount();$('#question').focus();
}
$('#query-form').onsubmit=e=>{e.preventDefault();sendQuestion($('#question').value.trim());};
$('#question').oninput=updateInput;
$('#question').onkeydown=e=>{if(e.key==='Enter' && !e.shiftKey && !e.isComposing){e.preventDefault();$('#query-form').requestSubmit();}};
$('#clear-chat').onclick=()=>requestDeleteSession(state.session);
$('#messages').onclick=async e=>{
  const article=e.target.closest('.message');
  if(e.target.closest('.copy-answer'))await copyAnswer(article.copyText ?? streamText(article.answer));
  const citation=e.target.closest('[data-source-id]');if(citation)openSource(article,citation.dataset.sourceId);
  if(e.target.closest('.retry-answer')){if(!state.busy)sendQuestion(article.query);}
  if(e.target.tagName==='IMG' && !e.target.closest('.message-head')){const src=safeImageURL(e.target.src);if(src){$('#preview-image').src=src;$('#image-dialog').showModal();}}
};
$('#close-image').onclick=()=>$('#image-dialog').close();
$('#close-source').onclick=()=>$('#source-dialog').close();
document.addEventListener('error',e=>{if(e.target.tagName==='IMG' && e.target.closest('.message')){e.target.alt='图片暂不可用';e.target.title='图片加载失败';}},true);

window.addEventListener('zhiku:new-chat',resetChat);
window.addEventListener('zhiku:open-session',event=>loadHistory(event.detail));
window.addEventListener('pagehide',()=>{state.loadVersion++;disconnect();});
window.addEventListener('zhiku:restore',()=>loadHistory(state.session));
// 等侧栏标题加载后恢复历史；用户已切换会话时不覆盖其新操作。
const initialLoadVersion=state.loadVersion;
sessionsReady.then(()=>{if(!authState.expired && state.loadVersion===initialLoadVersion)loadHistory(state.session);});
