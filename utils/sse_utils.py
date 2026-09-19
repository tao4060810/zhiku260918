"""重放 SSE 事件，避免在工作线程之间共享 asyncio 队列。"""

import asyncio
import json
import time
from enum import StrEnum
from starlette.concurrency import run_in_threadpool
from pymongo.errors import PyMongoError
from utils.auth_utils import LoginRequired
from utils.knowledge_access import AccessDenied

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


async def sse_generator(task_id, request, last_event_id=0, validate=None):
    """持续发送任务事件，任务结束或客户端断开时退出。

    :param task_id: 要订阅的任务 ID。
    :param request: 用于检查客户端是否断开的请求对象。
    :param last_event_id: 上次接收的事件编号，默认从头读取保留的事件。
    :param validate: 可选的同步权限检查函数，长连接期间定期执行。
    """
    cursor = last_event_id
    checked = None
    heartbeat = time.monotonic()

    async def check_access():
        """每隔至少两秒重新鉴权，失败时返回前端可识别的错误信息。"""
        nonlocal checked
        now = time.monotonic()
        if not validate or (checked is not None and now - checked < 2):
            return None
        try:
            # 数据库检查在线程池执行，超时则结束本次流，避免无权限时继续发送。
            await asyncio.wait_for(run_in_threadpool(validate), timeout=3)
        except (LoginRequired, AccessDenied):
            return {"error": "登录或访问权限已失效。", "code": "AUTH_EXPIRED"}
        except (PyMongoError, asyncio.TimeoutError):
            return {"error": "认证服务暂不可用，请重新连接。", "code": "AUTH_UNAVAILABLE"}
        checked = now
        return None

    while not await request.is_disconnected():
        now = time.monotonic()
        if error := await check_access():
            yield encode_event(SSEEvent.ERROR, error)
            return
        # 1. 读取上次编号之后的新事件。
        events = read_events(task_id, cursor)
        # 2. 逐条发送给前端，发送结束事件后退出。
        for sequence, event, data in events:
            if error := await check_access():
                yield encode_event(SSEEvent.ERROR, error)
                return
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

        # 4. 空闲时每隔约 15 秒发送 SSE 注释心跳，随后异步等待，避免空转
        if now - heartbeat >= 15:
            yield ": heartbeat\n\n"
            heartbeat = now
        await asyncio.sleep(0.1)
