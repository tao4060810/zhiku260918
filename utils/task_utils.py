"""管理单进程内的任务状态和可重放事件历史，支持线程安全访问。"""

from collections import deque
from copy import deepcopy
from threading import RLock
from time import time
from uuid import uuid4

TASK_STATUS_PROCESSING = "processing"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TERMINAL = {"completed", "failed"}
_lock = RLock()
_tasks = {}
MAX_ACTIVE_TASKS = 20
MAX_RETAINED_TASKS = 500
RETENTION_SECONDS = 86400


class TaskConflict(ValueError):
    pass


def create_task(kind, *, session_id=None, filename=None, query=None):
    with _lock:
        now = time()
        for key, task in list(_tasks.items()):
            if task["status"] in TERMINAL and now - task["updated_at"] > RETENTION_SECONDS:
                del _tasks[key]
        active = [t for t in _tasks.values() if t["status"] not in TERMINAL]
        if session_id and any(t["session_id"] == session_id for t in active):
            raise TaskConflict("该会话仍有问题正在处理，请稍后再试。")
        if len(active) >= MAX_ACTIVE_TASKS:
            raise TaskConflict("任务队列已满，请稍后再试。")
        while len(_tasks) >= MAX_RETAINED_TASKS:
            oldest = next((k for k, t in _tasks.items() if t["status"] in TERMINAL), None)
            if oldest is None:
                break
            del _tasks[oldest]
        task_id = str(uuid4())
        _tasks[task_id] = {
            "task_id": task_id, "kind": kind, "session_id": session_id,
            "filename": filename, "query": query, "status": "pending",
            "done_list": [], "running_list": [], "error": None,
            "warnings": [], "result": {}, "created_at": now, "updated_at": now,
            "events": deque(maxlen=4096), "sequence": 0,
        }
        return task_id


def get_task(task_id):
    with _lock:
        task = _tasks.get(task_id)
        return deepcopy({k: v for k, v in task.items() if k not in {"events", "sequence"}}) if task else None


def list_tasks(kind=None):
    with _lock:
        return [get_task(k) for k, v in reversed(list(_tasks.items())) if kind is None or v["kind"] == kind]


def latest_session_task(session_id):
    with _lock:
        return next((get_task(k) for k, v in reversed(list(_tasks.items())) if v["session_id"] == session_id), None)


def publish_event(task_id, event, data):
    with _lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        task["sequence"] += 1
        task["events"].append((task["sequence"], str(event), deepcopy(data)))
        task["updated_at"] = time()


def read_events(task_id, after=0):
    with _lock:
        task = _tasks.get(task_id)
        return deepcopy([e for e in task["events"] if e[0] > after]) if task else []


def update_task_status(task_id, status, is_stream=False, error=None):
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
    task = _tasks[task_id]
    publish_event(task_id, "progress", {k: task[k] for k in ("status", "done_list", "running_list", "warnings")})


def add_running_task(task_id, name, is_stream=False):
    with _lock:
        task = _tasks.get(task_id)
        if task and name not in task["running_list"]:
            task["running_list"].append(name)
            _progress(task_id)


def add_done_task(task_id, name, is_stream=False):
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
    with _lock:
        if task_id in _tasks:
            _tasks[task_id]["warnings"].append(message)
            _progress(task_id)


def set_task_result(task_id, key, value):
    with _lock:
        if task_id in _tasks:
            _tasks[task_id]["result"][key] = deepcopy(value)


def get_task_result(task_id, key, default=None):
    task = get_task(task_id)
    return task["result"].get(key, default) if task else default
