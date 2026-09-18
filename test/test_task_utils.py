"""验证任务清理、查询顺序和数据副本，避免简化写法后改变原有行为。"""

import unittest
from unittest.mock import patch

from utils import task_utils as tasks


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(tasks, "_tasks", {}))

    def test_queries_return_latest_first_and_independent_data(self):
        first = tasks.create_task("query", session_id="chat")
        tasks.update_task_status(first, "completed")
        tasks.create_task("import", filename="manual.md")
        latest = tasks.create_task("query", session_id="chat")
        tasks.set_task_result(latest, "sources", [{"title": "说明书"}])

        records = tasks.list_tasks("query")
        self.assertEqual([record["task_id"] for record in records], [latest, first])
        self.assertEqual(tasks.latest_session_task("chat")["task_id"], latest)
        self.assertIsNone(tasks.latest_session_task("missing"))
        self.assertNotIn("events", records[0])
        records[0]["result"]["sources"][0]["title"] = "外部修改"
        self.assertEqual(tasks.get_task(latest)["result"]["sources"][0]["title"], "说明书")

    def test_capacity_cleanup_keeps_active_task(self):
        with patch.object(tasks, "MAX_RETAINED_TASKS", 2):
            active = tasks.create_task("query")
            completed = tasks.create_task("import")
            tasks.update_task_status(completed, "completed")
            newest = tasks.create_task("import")
        self.assertIsNotNone(tasks.get_task(active))
        self.assertIsNone(tasks.get_task(completed))
        self.assertEqual([task["task_id"] for task in tasks.list_tasks()], [newest, active])

    def test_expiration_only_removes_finished_tasks(self):
        with patch.object(tasks, "time", return_value=0):
            active = tasks.create_task("query")
            failed = tasks.create_task("import")
            tasks.update_task_status(failed, "failed", error="测试失败")
        with patch.object(tasks, "time", return_value=tasks.RETENTION_SECONDS + 1):
            tasks.create_task("import")
        self.assertIsNotNone(tasks.get_task(active))
        self.assertIsNone(tasks.get_task(failed))
