"""管理单进程内的任务状态和可重放事件历史，支持线程安全访问。"""

from collections import deque
from copy import deepcopy
from threading import RLock
from time import time
from uuid import uuid4
from typing import Any

TERMINAL = {"completed", "failed", "canceled"}  # 终止状态
# 同一线程会在更新任务时继续调用事件函数，因此使用可重复获取的锁。
_lock = RLock()
_tasks: dict[str, dict[str, Any]] = {}
# 按 (用户 ID, 上传批次 ID) 保存取消时间，文件尚未创建任务时也能记录取消。
_canceled_uploads: dict[tuple[str, str], float] = {}
MAX_ACTIVE_TASKS = 20
MAX_RETAINED_TASKS = 500
RETENTION_SECONDS = 86400


class TaskCanceled(Exception):
    """后台节点发现用户已终止任务。"""


def is_task_canceled(task_id):
    """
    判断任务是否已请求终止，供后台在节点边界停止后续处理
    :param task_id: 后台任务 ID
    :return: 正在终止或已经终止时为 True；任务不存在时为 False
    """
    with _lock:
        return bool(_tasks.get(task_id) and _tasks[task_id]["status"] in {"canceling", "canceled"})


def cancel_task(task_id, *, reason="用户终止导入"):
    """
    请求终止任务，不强行中断正在执行的外部调用；调用方负责校验任务归属
    :param task_id: 要终止的后台任务 ID
    :param reason: 写入任务记录的终止原因
    :return: 更新后的任务副本；已结束任务保持原状，不存在时返回 None
    """
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return None
        if task["status"] in TERMINAL or task["status"] == "canceling":
            return get_task(task_id)
        # 清理收尾前仍占用活动任务名额，避免反复取消绕过队列容量限制。
        task["status"] = "canceling"
        task["error"] = reason
        task["updated_at"] = time()
        _progress(task_id)
        return get_task(task_id)


def cancel_upload(user_id, upload_id):
    """
    记录上传批次的取消，并通知该批次已创建的导入任务
    :param user_id: 认证后取得的用户 ID，不能直接采用客户端传入的身份
    :param upload_id: 与上传请求对应的批次 UUID 字符串
    :raises ValueError: 取消标记达到容量上限，未接收本次新标记
    """
    with _lock:
        # 1. 在新增取消标记时清理过期记录，并限制内存占用。
        now = time()
        for key, created_at in list(_canceled_uploads.items()):
            if now - created_at > RETENTION_SECONDS:
                del _canceled_uploads[key]
        key = (user_id, upload_id)
        if key not in _canceled_uploads and len(_canceled_uploads) >= MAX_RETAINED_TASKS * 10:
            raise ValueError("终止请求过多，请稍后重试。")
        # 2. 与上传创建任务共用锁：尚未创建的任务被标记拦截，已创建的任务逐个终止。
        _canceled_uploads[key] = now
        for task in _tasks.values():
            if task["user_id"] == user_id and task.get("upload_id") == upload_id and task["kind"] == "import":
                cancel_task(task["task_id"])


def is_upload_canceled(user_id, upload_id):
    """
    查询当前用户的上传取消标记，不影响使用相同批次 ID 的其他用户
    :param user_id: 当前上传用户 ID
    :param upload_id: 可选批次 UUID 字符串；未提供时不使用批次取消
    :return: 当前进程中存在对应取消标记时为 True
    """
    with _lock:
        return upload_id is not None and (user_id, upload_id) in _canceled_uploads


def create_task(kind, *, user_id, kb_id, session_id=None, filename=None, query=None, document_id=None, size_bytes=0, upload_id=None):
    # 清理旧任务并检查会话冲突和容量限制，创建任务后返回任务 ID。
    with _lock:
        if not user_id or not kb_id:
            raise ValueError("任务缺少所属用户或知识库。")
        now = time()
        for key, task in list(_tasks.items()):
            # 清理已完成、失败或已终止且超时的任务；正在终止的任务仍需保留。
            if task["status"] in TERMINAL and now - task["updated_at"] > RETENTION_SECONDS:
                del _tasks[key]
        # 统计全部未结束任务，同时检查当前会话是否已有任务。
        active_count = 0
        for task in _tasks.values():
            if task["status"] in TERMINAL:
                continue
            active_count += 1
            if session_id and task["session_id"] == session_id and task["user_id"] == user_id:
                raise ValueError("该会话仍有问题正在处理，请稍后再试。")
        # 同一用户最多保留一个未结束问答、十个未结束导入，避免占满全局队列。
        own_active = [t for t in _tasks.values() if t["user_id"] == user_id and t["status"] not in TERMINAL]
        if kind == "query" and any(t["kind"] == "query" for t in own_active):
            raise ValueError("您的上一条问题仍在处理中，请稍后再试。")
        if kind == "import" and sum(t["kind"] == "import" for t in own_active) >= 10:
            raise ValueError("您的导入任务已达上限。")
        if active_count >= MAX_ACTIVE_TASKS:
            raise ValueError("任务队列已满，请稍后再试。")
        # 记录过多时，按创建顺序删除已结束的旧任务，为新任务留出位置。
        for key, task in list(_tasks.items()):
            if len(_tasks) < MAX_RETAINED_TASKS:
                break
            if task["status"] in TERMINAL:
                del _tasks[key]

        # 创建新任务
        task_id = str(uuid4())
        _tasks[task_id] = {
            "task_id": task_id,
            "user_id": user_id,
            "kb_id": kb_id,
            "document_id": document_id,
            "upload_id": upload_id,
            "size_bytes": size_bytes,
            "kind": kind,
            "session_id": session_id,
            "filename": filename,
            "query": query,
            "status": "pending",
            "done_list": [],
            "running_list": [],
            "error": None,
            "warnings": [],
            "result": {},
            "created_at": now,
            "updated_at": now,
            "events": deque(maxlen=4096),
            "sequence": 0,
        }
        return task_id


def get_task(task_id):
    # 返回任务信息的独立副本，不包含事件历史和事件编号；不存在时返回 None。
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return None
        info = {key: value for key, value in task.items() if key not in {"events", "sequence"}}
        return deepcopy(info)


def list_tasks(kind=None, *, user_id=None):
    # 按创建顺序从新到旧返回任务列表，可按任务类型和所属用户筛选。
    with _lock:
        items = []
        for task_id in reversed(_tasks):
            if (kind is None or _tasks[task_id]["kind"] == kind) and (user_id is None or _tasks[task_id]["user_id"] == user_id):
                items.append(get_task(task_id))
        return items


def latest_session_task(session_id, *, user_id):
    # 获取指定用户在会话中最近创建的任务，没有匹配归属的记录时返回 None。
    with _lock:
        for task_id in reversed(_tasks):
            if _tasks[task_id]["session_id"] == session_id and _tasks[task_id]["user_id"] == user_id:
                return get_task(task_id)
        return None


def publish_event(task_id, event, data):
    # 为事件分配递增编号并保存数据副本，供前端读取和断线后重放。
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        task["sequence"] += 1
        task["events"].append((task["sequence"], str(event), deepcopy(data)))
        task["updated_at"] = time()


def read_events(task_id, after=0):
    # 返回编号大于 after 的事件副本，用于读取尚未接收的事件。
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return []
        events = [event for event in task["events"] if event[0] > after]
        return deepcopy(events)


def update_task_status(task_id, status, error=None):
    # 更新任务状态和错误信息，结束时清空运行节点，并记录进度事件。
    with _lock:
        task = _tasks.get(task_id)
        # 迟到的节点更新不能覆盖取消状态；canceling 由 finish_task 统一收尾。
        if task is None or task["status"] in {"canceling", "canceled"}:
            return
        task["status"] = status
        task["error"] = error
        task["updated_at"] = time()
        if status in TERMINAL:
            task["running_list"] = []
        _progress(task_id)


def _progress(task_id):
    # 将当前状态、已完成节点、运行节点和警告保存为一条进度事件。
    task = _tasks[task_id]
    publish_event(task_id, "progress", {
        "status": task["status"],
        "done_list": task["done_list"],
        "running_list": task["running_list"],
        "warnings": task["warnings"],
    })


def add_running_task(task_id, name):
    # 将节点加入运行列表，避免重复添加，并记录进度变化。
    with _lock:
        task = _tasks.get(task_id)
        if task and task["status"] not in {"canceling", "canceled"} and name not in task["running_list"]:
            task["running_list"].append(name)
            _progress(task_id)


def add_done_task(task_id, name):
    # 将节点移出运行列表并标记为已完成，然后记录进度变化。
    with _lock:
        task = _tasks.get(task_id)
        if not task or task["status"] in {"canceling", "canceled"}:
            return
        if name in task["running_list"]:
            task["running_list"].remove(name)
        if name not in task["done_list"]:
            task["done_list"].append(name)
        _progress(task_id)


def add_task_warning(task_id, message):
    # 添加任务警告，并通过进度事件提供给前端展示。
    with _lock:
        if task_id in _tasks:
            _tasks[task_id]["warnings"].append(message)
            _progress(task_id)


def set_task_result(task_id, key, value):
    # 按键保存任务结果的独立副本，避免外部修改影响已保存的数据。
    with _lock:
        if task_id in _tasks:
            _tasks[task_id]["result"][key] = deepcopy(value)


def finish_task(task_id, *, result=None, error=None):
    """
    在同一把锁内发布结果、结束状态和事件，使 SSE 和轮询读取一致快照
    :param task_id: 要结束的任务 ID
    :param result: 可选的完整结果字典，提供时替换任务原结果
    :param error: 可选错误说明；未请求取消时，有错误标记失败，否则标记完成
    """
    with _lock:
        if task_id not in _tasks or _tasks[task_id]["status"] == "canceled":
            return
        # 工作流停止并尝试清理后确认终止，丢弃迟到的结果并保留用户的取消原因。
        if _tasks[task_id]["status"] == "canceling":
            task = _tasks[task_id]
            task["status"] = "canceled"
            task["running_list"] = []
            _progress(task_id)
            publish_event(task_id, "error", {"error": task["error"], "code": "TASK_CANCELED", "task_id": task_id})
            return
        if result is not None:
            _tasks[task_id]["result"] = deepcopy(result)
        update_task_status(task_id, "failed" if error else "completed", error=error)
        task = get_task(task_id)
        data = {"error": error, "task_id": task_id} if error else {
            **task["result"], "task_id": task_id, "status": "completed",
            "done_list": task["done_list"], "warnings": task["warnings"],
        }
        publish_event(task_id, "error" if error else "final", data)
