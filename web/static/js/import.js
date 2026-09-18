/* 导入页：文件选择、上传进度、任务筛选与轮询。公共能力由 common.js 提供。 */
Object.assign(state,{tasks:[],files:[],filter:'all',uploading:false});
function queueFiles(files) {
  if(state.uploading || isImportLocked())return;
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
  if(state.uploading || isImportLocked() || !state.files.length)return;
  state.uploading=true;beginImportUpload();renderQueue();$('#upload-meter').hidden=false;$('#upload-progress').value=0;$('#upload-percent').textContent='0%';
  const form=new FormData();state.files.forEach(f=>form.append('files',f));
  const xhr=new XMLHttpRequest();xhr.open('POST','/upload');xhr.timeout=180000;
  xhr.upload.onprogress=e=>{if(e.lengthComputable){const p=Math.round(e.loaded/e.total*100);updateImportUpload(p);$('#upload-progress').value=p;$('#upload-percent').textContent=p===100?'正在接收…':`${p}%`;}};
  xhr.onload=()=>{let data;try{data=JSON.parse(xhr.responseText);}catch{data={detail:'上传失败。'};}
    if(xhr.status>=200 && xhr.status<300){acceptImportTasks(data.task_ids,state.files);state.files=[];}
    else toast(typeof data.detail==='string'?data.detail:'上传失败，请重试。');
  };
  xhr.onerror=()=>toast('上传连接失败，请确认后端已启动并检查网络连接。');xhr.ontimeout=()=>toast('上传超时，请重试。');
  xhr.onloadend=()=>{state.uploading=false;$('#upload-meter').hidden=true;$('#files').value='';renderQueue();finishImportUpload();};xhr.send(form);
}
function renderTasks() {
  $('#stat-total').innerHTML=`${state.tasks.length}<small>份</small>`;$('#stat-done').innerHTML=`${state.tasks.filter(t=>t.status==='completed').length}<small>份</small>`;$('#stat-running').innerHTML=`${state.tasks.filter(t=>['pending','processing'].includes(t.status)).length}<small>份</small>`;
  const filter=$('#task-search').value.toLowerCase();
  const tasks=state.tasks.filter(t=>(state.filter==='all' || (state.filter==='processing'?['pending','processing'].includes(t.status):t.status===state.filter)) && t.filename?.toLowerCase().includes(filter));
  const open=new Set($$('.task-item[open]').map(x=>x.dataset.task));
  $('#task-list').innerHTML=tasks.length?tasks.map(task=>`<details class="task-item" data-task="${task.task_id}" ${open.has(task.task_id)?'open':''}><summary class="task-summary"><div class="file-name"><span class="file-icon ${/\.pdf$/i.test(task.filename)?'pdf':''}">${icon('file-text')}</span><div><strong title="${escapeHTML(task.filename)}">${escapeHTML(task.filename)}</strong><small>${task.result.chunk_count!==undefined?`${task.result.chunk_count} 个切片`:(task.running_list.map(n=>labels[n]||n).join(' · ') || statusNames[task.status])}</small></div></div><span class="status-label ${task.status}"><span class="status-dot ${task.status==='completed'?'good':task.status==='failed'?'bad':'amber'}"></span>${statusNames[task.status]}</span><span class="task-time">${new Date(task.created_at*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</span>${icon('chevron-down')}</summary><div class="task-detail"><div class="progress-steps">${task.done_list.map(n=>`<span class="progress-step">${icon('check')}${escapeHTML(labels[n]||n)}</span>`).join('')}${task.running_list.map(n=>`<span class="progress-step">${icon('loader-circle')}${escapeHTML(labels[n]||n)}</span>`).join('')}</div>${task.error?`<p class="warning-line">${escapeHTML(task.error)}</p><button class="text-button retry-import" data-filename="${escapeHTML(task.filename)}">${icon('upload')}重新选择文件</button>`:''}${task.warnings.map(w=>`<p class="warning-line">${escapeHTML(w)}</p>`).join('')}${task.result.item_name?`<p class="muted small" style="margin-top:12px">${escapeHTML(task.result.item_name)}</p>`:''}</div></details>`).join(''):`<div class="task-empty">${icon('files')}<span>${state.tasks.length?'暂无匹配的文档':'暂无导入记录'}</span></div>`;
  icons();
}
function loadTasks() {
  return refreshImportTasks();
}
// 使用公共轮询的结果，任务列表与界面锁定始终基于同一份后台状态。
window.addEventListener('zhiku:import-tasks',event=>{state.tasks=event.detail;renderTasks();});
$('#choose-files').onclick=()=>$('#files').click();$('#files').onchange=e=>queueFiles(e.target.files);
$('#upload-queue').onclick=e=>{const button=e.target.closest('[data-remove]');if(button && !state.uploading){state.files.splice(Number(button.dataset.remove),1);renderQueue();}};
$('#dropzone').ondragover=e=>{e.preventDefault();if(!state.uploading)$('#dropzone').classList.add('dragging');};
$('#dropzone').ondragleave=()=>$('#dropzone').classList.remove('dragging');
$('#dropzone').ondrop=e=>{e.preventDefault();$('#dropzone').classList.remove('dragging');queueFiles(e.dataTransfer.files);};
$('#upload-button').onclick=uploadFiles;$('#refresh-tasks').onclick=loadTasks;
$('#task-search').oninput=renderTasks;
$$('[data-filter]').forEach(b=>b.onclick=()=>{state.filter=b.dataset.filter;$$('[data-filter]').forEach(x=>x.setAttribute('aria-selected',String(x===b)));renderTasks();});
$('#task-list').onclick=e=>{if(e.target.closest('.retry-import'))$('#files').click();};

// 页面跳转会中断仍在传输的文件；已提交的后台导入任务不受影响。
window.addEventListener('beforeunload',event=>{if(state.uploading){event.preventDefault();event.returnValue='';}});
