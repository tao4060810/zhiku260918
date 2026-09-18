"""重放 SSE 事件，避免在工作线程之间共享 asyncio 队列。"""

import asyncio
import json
from enum import StrEnum
from time import monotonic

from utils.task_utils import TERMINAL, get_task, publish_event, read_events


class SSEEvent(StrEnum):
    READY = "ready"
    PROGRESS = "progress"
    DELTA = "delta"
    FINAL = "final"
    ERROR = "error"


def push_to_session(task_id, event, data):
    publish_event(task_id, event, data)


def encode_event(event, data, event_id=None):
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def sse_generator(task_id, request, last_event_id=0):
    yield encode_event(SSEEvent.READY, {"task_id": task_id})
    cursor = last_event_id
    heartbeat = monotonic()
    while not await request.is_disconnected():
        events = read_events(task_id, cursor)
        for sequence, event, data in events:
            cursor = sequence
            yield encode_event(event, data, sequence)
            if event in (SSEEvent.FINAL, SSEEvent.ERROR):
                return
        task = get_task(task_id)
        if task is None:
            yield encode_event(SSEEvent.ERROR, {"error": "任务已过期，请重新提问。"})
            return
        if not events and task["status"] in TERMINAL:
            event = SSEEvent.FINAL if task["status"] == "completed" else SSEEvent.ERROR
            yield encode_event(event, task["result"] if event == SSEEvent.FINAL else {"error": task["error"]})
            return
        if monotonic() - heartbeat >= 15:
            yield ": heartbeat\n\n"
            heartbeat = monotonic()
        await asyncio.sleep(0.1)
