"""重放 SSE 事件，避免在工作线程之间共享 asyncio 队列。"""

import asyncio
import json
from enum import StrEnum

from utils.task_utils import TERMINAL, get_task, publish_event, read_events


class SSEEvent(StrEnum):
    PROGRESS = "progress"
    DELTA = "delta"
    FINAL = "final"
    ERROR = "error"


def push_to_session(task_id, event, data):
    # 将事件保存到指定任务的历史记录中，供 SSE 连接读取。
    publish_event(task_id, event, data)


def encode_event(event, data, event_id=None):
    # 将事件类型、数据和可选编号编码为浏览器可接收的 SSE 消息。
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def sse_generator(task_id, request, last_event_id=0):
    """持续发送任务事件，任务结束或客户端断开时退出。

    :param task_id: 要订阅的任务 ID。
    :param request: 用于检查客户端是否断开的请求对象。
    :param last_event_id: 上次接收的事件编号，默认从头读取保留的事件。
    """
    cursor = last_event_id
    while not await request.is_disconnected():
        # 1. 读取上次编号之后的新事件。
        events = read_events(task_id, cursor)
        # 2. 逐条发送给前端，发送结束事件后退出。
        for sequence, event, data in events:
            cursor = sequence
            yield encode_event(event, data, sequence)
            if event in (SSEEvent.FINAL, SSEEvent.ERROR):
                return
        if events:
            continue

        # 3. 没有新事件时检查任务；已结束或不存在时通知前端并退出。
        task = get_task(task_id)
        if task is None:
            yield encode_event(SSEEvent.ERROR, {"error": "任务已过期，请重新提问。"})
            return
        if task["status"] in TERMINAL:
            if task["status"] == "completed":
                yield encode_event(SSEEvent.FINAL, task["result"])
            else:
                yield encode_event(SSEEvent.ERROR, {"error": task["error"]})
            return

        # 4. 任务仍在运行，异步等待后再读取，避免空转占用 CPU。
        await asyncio.sleep(0.1)
