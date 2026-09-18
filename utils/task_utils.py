"""管理单进程内的任务状态和可重放事件历史，支持线程安全访问。"""

from collections import deque
from copy import deepcopy
from threading import RLock
from time import time
from uuid import uuid4

TERMINAL = {"completed", "failed"}  # 终止状态
# 同一线程会在更新任务时继续调用事件函数，因此使用可重复获取的锁。
_lock = RLock()
_tasks = {}
MAX_ACTIVE_TASKS = 20
MAX_RETAINED_TASKS = 500
RETENTION_SECONDS = 86400


def create_task(kind, *, session_id=None, filename=None, query=None):
    # 清理旧任务并检查会话冲突和容量限制，创建任务后返回任务 ID。
    with _lock:
        now = time()
        for key, task in list(_tasks.items()):
            # 清理已完成或失败且超时的任务
            if task["status"] in TERMINAL and now - task["updated_at"] > RETENTION_SECONDS:
                del _tasks[key]
        # 统计全部未结束任务，同时检查当前会话是否已有任务。
        active_count = 0
        for task in _tasks.values():
            if task["status"] in TERMINAL:
                continue
            active_count += 1
            if session_id and task["session_id"] == session_id:
                raise ValueError("该会话仍有问题正在处理，请稍后再试。")
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


def list_tasks(kind=None):
    # 按创建顺序从新到旧返回任务列表，可按任务类型筛选。
    with _lock:
        items = []
        for task_id in reversed(_tasks):
            if kind is None or _tasks[task_id]["kind"] == kind:
                items.append(get_task(task_id))
        return items


def latest_session_task(session_id):
    # 获取指定会话最近创建的任务，没有记录时返回 None。
    with _lock:
        for task_id in reversed(_tasks):
            if _tasks[task_id]["session_id"] == session_id:
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
        if task is None:
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
        if task and name not in task["running_list"]:
            task["running_list"].append(name)
            _progress(task_id)


def add_done_task(task_id, name):
    # 将节点移出运行列表并标记为已完成，然后记录进度变化。
    with _lock:
        task = _tasks.get(task_id)
        if not task:
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
