"""测试使用内存 Mongo 替身；不连接项目 .env 中的真实数据库。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import mongomock
from fastapi.testclient import TestClient
from utils import user_store, task_utils, rate_limit_utils
from utils.auth_utils import passwords
from utils.knowledge_store import ensure_default_kb
from web.app import app

PASSWORD = "test-password-2026"
ORIGIN = "http://localhost:8000"


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        """
        为每个测试创建独立内存数据库、任务记录和临时上传目录
        """
        self.db = mongomock.MongoClient(tz_aware=True).test
        self.enterContext(patch.object(user_store, "_db", self.db))
        for collection, field in [("users", "username"), ("knowledge_bases", "owner_user_id"), ("auth_sessions", "token_hash")]:
            self.db[collection].create_index(field, unique=True)
        self.db.documents.create_index([("kb_id", 1), ("file_title", 1)], unique=True)
        self.enterContext(patch.object(task_utils, "_tasks", {}))
        self.enterContext(patch.object(task_utils, "_canceled_uploads", {}))
        self.enterContext(patch.object(rate_limit_utils, "_buckets", {}))
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(folder)
        self.enterContext(patch("web.api.import_service.data_root", return_value=self.root))
        self.client = self.enterContext(TestClient(app, base_url=ORIGIN))

    def account(self, name="alice", role="user"):
        """
        创建测试账号及默认知识库
        :param name: 测试用户名
        :param role: 测试账号角色
        :return: 测试用户和默认知识库记录
        """
        user = user_store.create_user(name, passwords.hash(PASSWORD), role)
        return user, ensure_default_kb(user["_id"])

    def login(self, name="alice", client=None):
        """
        通过真实登录路由登录测试账号，设置后续写请求的 CSRF 头
        :param name: 要登录的测试用户名
        :param client: 可选测试客户端，用于模拟不同浏览器
        :return: 登录接口响应
        """
        client = client or self.client
        client.headers["Origin"] = ORIGIN
        response = client.post("/auth/login", json={"username": name, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return response

    def chat(self, user, kb, session_id=None):
        """
        创建具有明确用户及知识库归属的测试会话
        :param user: 通过认证依赖取得的当前用户记录
        :param kb: 测试知识库记录
        :param session_id: 可选固定会话 ID，省略时自动生成
        :return: 创建的会话 ID
        """
        session_id = session_id or str(uuid4())
        self.db.chat_sessions.insert_one({"_id": session_id, "user_id": user["_id"], "kb_id": kb["_id"]})
        return session_id
