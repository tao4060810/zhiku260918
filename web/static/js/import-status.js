/* 两个页面共用导入状态：上传、排队、解析、入库期间锁定界面，可终止当前账号的导入。 */
const importState = {checking:true, uploading:false, canceling:false, uploadText:'', tasks:[], batch:new Set(), error:'', timer:null, version:0, stopped:false};
// canceling 表示后台尚在停止和清理，不能提前关闭弹窗或允许下一次上传。
const importActive = task => ['pending','processing','canceling'].includes(task.status);
statusNames.canceling = '正在终止';
statusNames.canceled = '已终止';
function isImportLocked() {
  return importState.checking || importState.uploading || importState.canceling || importState.tasks.some(importActive);
}
function renderImportStatus() {
  const dialog = $('#import-status-dialog');
  if (!isImportLocked()) {
    if (dialog.open) dialog.close();
    return;
  }
  $('#import-status-title').textContent = importState.uploading ? '正在上传文件…' : importState.checking ? '正在确认导入状态…' : '正在导入中…';
  const cancelable = importState.tasks.some(task => ['pending','processing'].includes(task.status));
  $('#import-cancel').textContent = importState.canceling || (!cancelable && importState.tasks.some(task=>task.status==='canceling')) ? '正在终止…' : importState.uploading ? '取消上传' : '终止上传';
  $('#import-cancel').disabled = importState.canceling || (!importState.uploading && !cancelable);
  $('#import-status-description').textContent = importState.uploading ? importState.uploadText : '导入期间暂时无法进行其他操作，全部完成、失败或终止后将自动恢复。';
  // 同一批次中已完成、失败的文件也保留显示，方便了解整批导入的进展。
  const tasks = importState.tasks.filter(task => importState.batch.has(task.task_id));
  const html = tasks.map(task => `<div class="import-status-item"><strong>${escapeHTML(task.filename)}</strong><span>${escapeHTML(statusNames[task.status])}</span><p>${escapeHTML(task.running_list.map(name => labels[name] || name).join(' · ') || (task.status==='pending' ? '等待前面的任务完成' : task.error || ''))}</p><small>${escapeHTML(task.done_list.map(name => labels[name] || name).join(' → '))}</small></div>`).join('');
  // 没有进度变化时不重建内容，避免屏幕阅读器重复播报。
  if ($('#import-status-list').innerHTML !== html) $('#import-status-list').innerHTML = html;
  $('#import-status-error').hidden = !importState.error;
  $('#import-status-error').textContent = importState.error;
  if (!dialog.open) dialog.showModal();
}
function beginImportUpload() {
  importState.version++; // 忽略上传开始前发出的旧查询，避免旧结果覆盖新任务。
  importState.batch.clear();
  importState.uploading = true;
  importState.uploadText = '正在上传，请保持页面打开。';
  importState.error = '';
  renderImportStatus();
}
function updateImportUpload(percent) {
  importState.uploadText = percent === 100 ? '文件已传输，等待服务器接收并创建导入任务…' : `已上传 ${percent}%，请保持页面打开。`;
  renderImportStatus();
}
function acceptImportTasks(taskIds, files) {
  importState.version++;
  // 上传响应与下一次轮询之间也保持锁定，不让用户再次点击提交。
  taskIds.forEach((id,index) => {
    importState.batch.add(id);
    importState.tasks.push({task_id:id,filename:files[index]?.name || '文档',status:'pending',running_list:[],done_list:[]});
  });
}
function finishImportUpload() {
  importState.uploading = false;
  // 即使上传连接中断，后台也可能已接收文件；确认任务状态后再恢复操作。
  importState.checking = true;
  renderImportStatus();
  refreshImportTasks();
}
// 仅终止当前账号的上传或导入，不改变登录状态；已完成任务不再次提交取消。
async function cancelImport() {
  if (importState.canceling) return;
  const uploading = importState.uploading;
  const ids = importState.tasks.filter(task => importState.batch.has(task.task_id) && ['pending','processing'].includes(task.status)).map(task => task.task_id);
  if (!uploading && !ids.length) return;
  importState.canceling = true;
  renderImportStatus();
  try {
    if (uploading) {
      // 先在服务端记录批次取消，避免断开连接后已接收的文件继续导入。
      const xhr = state.uploadXHR;
      await api(`/uploads/${encodeURIComponent(state.uploadId)}/cancel`, {method:'POST'});
      xhr?.abort();
      importState.uploading = false;
      importState.checking = true;
    } else {
      await Promise.all(ids.map(id => api(`/tasks/${encodeURIComponent(id)}/cancel`, {method:'POST'})));
    }
    importState.error = '';
    toast('正在终止导入，已完成的文件不会回退。');
  } catch (error) {
    importState.error = error.message;
    toast(`终止失败：${error.message}`);
  } finally {
    // 请求结束不等于任务终止，解除按钮的请求锁后继续以后台轮询状态决定弹窗关闭。
    importState.canceling = false;
    renderImportStatus();
    refreshImportTasks();
  }
}
async function refreshImportTasks() {
  clearTimeout(importState.timer);
  const version = ++importState.version;
  try {
    const data = await api('/tasks', {signal:AbortSignal.timeout(10000)});
    if (version !== importState.version || importState.stopped) return;
    const wasActive = importState.tasks.some(importActive);
    importState.tasks = data.items;
    data.items.filter(importActive).forEach(task => importState.batch.add(task.task_id));
    importState.checking = false;
    importState.error = '';
    renderImportStatus();
    window.dispatchEvent(new CustomEvent('zhiku:import-tasks', {detail:data.items}));
    if (wasActive && !data.items.some(importActive) && !importState.uploading) {
      const failed = data.items.filter(task => importState.batch.has(task.task_id) && task.status==='failed').length;
      const canceled = data.items.filter(task => importState.batch.has(task.task_id) && task.status==='canceled').length;
      toast(canceled ? `已终止 ${canceled} 份导入。` : failed ? `导入已结束，${failed} 份文档失败，请在文档管理中查看原因。` : '导入已结束，可以继续操作。');
      importState.batch.clear();
    }
  } catch (error) {
    if (version !== importState.version || importState.stopped) return;
    // 查询失败不能当成导入完成；保留锁定状态并自动重试。
    importState.error = '暂时无法获取导入进度，正在自动重试。请检查后端及网络连接。';
    renderImportStatus();
  } finally {
    if (version === importState.version && !importState.stopped) importState.timer = setTimeout(refreshImportTasks,2500);
  }
}
// 禁止 Esc 关闭以及拖入文件触发浏览器跳转；背景控件由模态框自动隔离。
$('#import-status-dialog').addEventListener('cancel',event => event.preventDefault());
for (const type of ['dragover','drop']) $('#import-status-dialog').addEventListener(type,event => event.preventDefault());
window.addEventListener('pagehide',() => {
  importState.stopped = true;
  importState.version++;
  clearTimeout(importState.timer);
});
window.addEventListener('zhiku:restore',() => {
  importState.stopped = false;
  importState.checking = true;
  renderImportStatus();
  refreshImportTasks();
});
// 确认身份后才启动任务轮询；登录失效时停止计时器并使在途查询失效。
authReady.then(valid=>{if(valid){renderImportStatus();refreshImportTasks();}});
window.addEventListener('zhiku:auth-expired',()=>{importState.stopped=true;importState.version++;clearTimeout(importState.timer);});
$('#import-cancel').onclick=cancelImport;
$('#import-cancel').title='终止当前上传，不退出登录';
