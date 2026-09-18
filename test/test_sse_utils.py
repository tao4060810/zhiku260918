"""验证 SSE 在等待、结束和断开连接时的行为，不调用外部服务。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from utils import task_utils as tasks
from utils.sse_utils import sse_generator


class SSETests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_new_events_then_finish(self):
        task_id = tasks.create_task("query")
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

        async def produce_events(_):
            tasks.publish_event(task_id, "delta", {"delta": "你好"})
            tasks.publish_event(task_id, "final", {"answer": "你好"})
            tasks.update_task_status(task_id, "completed")

        with patch("utils.sse_utils.asyncio.sleep", side_effect=produce_events) as wait:
            messages = [message async for message in sse_generator(task_id, request)]
        self.assertEqual(len(messages), 2)
        self.assertIn("event: delta", messages[0])
        self.assertIn("event: final", messages[1])
        wait.assert_awaited_once_with(0.1)

    async def test_disconnected_client_does_not_cancel_task(self):
        task_id = tasks.create_task("query")
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
        messages = [message async for message in sse_generator(task_id, request)]
        self.assertEqual(messages, [])
        self.assertEqual(tasks.get_task(task_id)["status"], "pending")
        tasks.update_task_status(task_id, "completed")

    async def test_finished_task_without_new_events_sends_result(self):
        for status, event, data in (("completed", "final", "测试答案"), ("failed", "error", "模型离线")):
            with self.subTest(status=status):
                task_id = tasks.create_task("query")
                tasks.set_task_result(task_id, "answer", data)
                tasks.update_task_status(task_id, status, error=data if status == "failed" else None)
                cursor = tasks.read_events(task_id)[-1][0]
                request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
                messages = [message async for message in sse_generator(task_id, request, cursor)]
                self.assertEqual(len(messages), 1)
                self.assertIn(f"event: {event}", messages[0])
                self.assertIn(data, messages[0])

    async def test_missing_task_sends_error(self):
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        messages = [message async for message in sse_generator("missing-task", request)]
        self.assertEqual(len(messages), 1)
        self.assertIn("event: error", messages[0])
        self.assertIn("任务已过期", messages[0])
