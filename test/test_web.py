"""HTTP 接口与工作流契约测试；在外部服务调用处使用替身。"""

from contextlib import ExitStack
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from utils import task_utils as tasks
from utils.sse_utils import push_to_session
from web.app import app
from support import DatabaseCase
from pymongo.errors import ConnectionFailure


def fake_query(task_id, session_id, query, is_stream):
    tasks.update_task_status(task_id, "processing")
    tasks.add_running_task(task_id, "node_answer_output")
    push_to_session(task_id, "delta", {"delta": "测试"})
    tasks.set_task_result(task_id, "answer", "测试答案")
    tasks.set_task_result(task_id, "sources", [])
    tasks.add_done_task(task_id, "node_answer_output")
    tasks.update_task_status(task_id, "completed")
    push_to_session(task_id, "final", {"answer": "测试答案", "sources": []})


class WebTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.user,self.kb=self.account();self.login()
        self.temp=SimpleNamespace(name=str(self.root))
        self.chat(self.user,self.kb,'test');self.chat(self.user,self.kb,'busy')
        self.client.params={'kb_id':self.kb['_id']}

    def test_pages_and_assets(self):
        paths = ["/", "/chat.html", "/import.html", "/static/assets/brand.png", "/docs"]
        paths += [f"/static/js/{name}.js" for name in ("common", "import-status", "chat", "import")]
        paths += [f"/static/css/{name}.css" for name in ("base", "chat", "import")]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_pages_render_only_their_own_content_and_assets(self):
        # 通过真实模板响应检查页面隔离，避免拆分后仍偷偷依赖另一个页面的 DOM。
        for view, other, title in (("chat", "import", "知识问答"), ("import", "chat", "文档管理")):
            with self.subTest(view=view):
                response = self.client.get(f"/{view}.html")
                self.assertEqual(response.template.name, f"{view}.html")
                html = response.text
                self.assertIn(f"<title>掌柜智库 · {title}</title>", html)
                self.assertIn(f'id="{view}-view"', html)
                self.assertNotIn(f'id="{other}-view"', html)
                self.assertIn('/static/js/common.js', html)
                self.assertIn('/static/css/base.css', html)
                self.assertIn(f'/static/js/{view}.js', html)
                self.assertIn(f'/static/css/{view}.css', html)
                self.assertNotIn(f'/static/js/{other}.js', html)
                self.assertNotIn(f'/static/css/{other}.css', html)
                self.assertIn(f'href="/{view}.html" aria-current="page"', html)
                self.assertIn('id="confirm-dialog"', html)
                self.assertIn('id="import-status-dialog"', html)
                self.assertIn('id="import-cancel"', html)
                self.assertIn('id="logout"', html)
                self.assertIn('/static/js/import-status.js', html)
                self.assertEqual('id="source-dialog"' in html, view == "chat")
                self.assertEqual('/static/vendor/marked.umd.js' in html, view == "chat")
                self.assertNotIn('{% ', html)

    def test_query_validation(self):
        for body in ({"query":"  "}, {"query":"a" * 4001}, {"query":"test", "session_id":"../../bad"}):
            self.assertEqual(self.client.post("/query", json=body).status_code, 422)

    @patch("web.api.query_service.run_query_graph", side_effect=fake_query)
    def test_blocking_answer(self, mock_run):
        result = self.client.post("/query", json={"kb_id":self.kb["_id"],"query":"hello", "is_stream":False})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["answer"], "测试答案")
        self.assertEqual(result.json()["done_list"], ["node_answer_output"])
        mock_run.assert_called_once()

    @patch("web.api.query_service.run_query_graph", side_effect=fake_query)
    def test_sse_replay_and_resume(self, _):
        data = self.client.post("/query", json={"kb_id":self.kb["_id"],"query":"hello"}).json()
        # 等待实际执行器完成任务，确保连接建立前所有事件已写入缓冲区。
        app.state.executor.submit(lambda: None).result(timeout=5)
        url = f'/stream/{data["session_id"]}?task_id={data["task_id"]}'
        response = self.client.get(url)
        self.assertIn("event: delta", response.text)
        self.assertIn("测试答案", response.text)
        events = tasks.read_events(data["task_id"])
        delta_id = next(e[0] for e in events if e[1] == "delta")
        resumed = self.client.get(url, headers={"Last-Event-ID": str(delta_id)})
        self.assertNotIn("event: delta", resumed.text)
        self.assertIn("event: final", resumed.text)
        self.assertEqual(self.client.get('/stream/other?task_id='+data['task_id']).status_code,404)

    def test_same_session_rejects_overlap(self):
        tasks.create_task("query", user_id=self.user["_id"],kb_id=self.kb["_id"], session_id="busy")
        response = self.client.post("/query", json={"kb_id":self.kb["_id"],"query":"hello", "session_id":"busy"})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.client.delete('/history/busy').status_code,409)

    def test_failed_query_has_error_not_success(self):
        def fail(task_id, *args):
            tasks.update_task_status(task_id, "failed", error="模型离线")
            push_to_session(task_id, "error", {"error":"模型离线"})
        with patch("web.api.query_service.run_query_graph", side_effect=fail):
            response=self.client.post('/query',json={"kb_id":self.kb["_id"],"query":"test","is_stream":False})
        self.assertEqual(response.status_code,503)

    def test_invalid_uploads(self):
        for name, content, status in [("../test.md",b"text",400),("a.exe",b"text",400),("empty.md",b"",400),("a.pdf",b"not pdf",400),("a.md",b"\xff",400)]:
            with self.subTest(name=name):
                self.assertEqual(self.client.post('/upload',files={'files':(name,content)}).status_code,status)
        self.assertFalse(list(Path(self.temp.name).rglob('*.md')))

    @patch("web.api.import_service.MAX_FILE_BYTES", 4)
    def test_oversize_upload_is_cleaned_up(self):
        self.assertEqual(self.client.post('/upload',files={'files':('a.md',b'12345')}).status_code,413)
        self.assertFalse(list(Path(self.temp.name).rglob('*.md')))

    @patch("web.api.import_service.run_import_graph")
    def test_batch_upload_and_progress(self, run):
        response=self.client.post('/upload',files=[('files',('a.MD',b'# Hello')),('files',('b.pdf',b'%PDF-1.7\nfixture'))])
        self.assertEqual(response.status_code,202)
        app.state.executor.submit(lambda:None).result(timeout=5)
        self.assertEqual(run.call_count,2)
        for task_id in response.json()['task_ids']:
            task=self.client.get('/status/'+task_id).json()
            self.assertEqual(task['done_list'],['upload_file'])
        self.assertEqual(len(self.client.get('/tasks').json()['items']),2)

    def test_batch_validation_does_not_start_partial_work(self):
        with patch('web.api.import_service.run_import_graph') as run:
            response=self.client.post('/upload',files=[('files',('a.md',b'valid')),('files',('b.pdf',b'bad'))])
        self.assertEqual(response.status_code,400)
        run.assert_not_called()
        self.assertFalse(list(Path(self.temp.name).rglob('*.md')))

    @patch('utils.task_utils.MAX_ACTIVE_TASKS', 1)
    def test_batch_capacity_conflict_rolls_back(self):
        # 普通 ValueError 仍转换成 409，且整批任务、临时文件正确回滚。
        with patch('web.api.import_service.run_import_graph') as run:
            response = self.client.post('/upload', files=[('files', ('a.md', b'valid')), ('files', ('b.md', b'valid'))])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], '任务队列已满，请稍后再试。')
        run.assert_not_called()
        self.assertFalse(list(Path(self.temp.name).rglob('*.md')))
        self.assertTrue(all(task['status'] == 'failed' for task in tasks.list_tasks()))

    def test_history_returns_images_and_sources(self):
        records=[{'_id':123,'role':'assistant','text':'ok[cite:1]','image_urls':['/assets/00000000-0000-0000-0000-000000000001'],'sources':[{'title':'Manual','source_id':'1','source':'local','content':'![diagram](/assets/00000000-0000-0000-0000-000000000001)'}]}]
        with patch('web.api.query_service.history_store.get_recent_messages',return_value=records):
            data=self.client.get('/history/test').json()
        self.assertEqual(data['items'][0]['_id'],'123')
        self.assertEqual(data['items'][0]['sources'][0]['title'],'Manual')
        self.assertEqual(data['items'][0]['image_urls'],['/assets/00000000-0000-0000-0000-000000000001'])

    def test_database_error_is_not_empty_success(self):
        with patch('web.api.query_service.history_store.get_recent_messages',side_effect=ConnectionFailure()):
            self.assertEqual(self.client.get('/history/test').status_code,503)
        with patch('web.api.query_service.history_store.clear_history',side_effect=ConnectionFailure()):
            self.assertEqual(self.client.delete('/history/test').status_code,503)

    def test_missing_task_and_invalid_cursor(self):
        self.assertEqual(self.client.get('/status/missing').status_code,404)
        task=tasks.create_task('query',user_id=self.user['_id'],kb_id=self.kb['_id'],session_id='test')
        self.assertEqual(self.client.get('/stream/test?task_id='+task,headers={'Last-Event-ID':'bad'}).status_code,400)


class RegressionTests(DatabaseCase):
    def test_markdown_entry_reads_file(self):
        from processor.import_processor.nodes.node_entry import NodeEntry
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'manual.MD'
            path.write_text('# Test\nContent',encoding='utf-8')
            result=NodeEntry().process({'import_file_path':str(path)})
        self.assertEqual(result['md_content'],'# Test\nContent')

    def test_latest_history_in_chronological_order(self):
        from utils.mongo_history_utils import get_recent_messages
        cursor=unittest.mock.MagicMock()
        cursor.sort.return_value.limit.return_value=[{'text':'newest'},{'text':'previous'}]
        tool=SimpleNamespace(chat_message=SimpleNamespace(find=lambda query:cursor))
        with patch('utils.mongo_history_utils.get_db',return_value=tool), patch('utils.mongo_history_utils.require_chat'):
            result=get_recent_messages('test',2,user_id='a',kb_id='k')
        self.assertEqual([r['text'] for r in result],['previous','newest'])
        cursor.sort.assert_called_once_with([('ts',-1),('_id',-1)])

    def test_graph_joins_all_three_branches_once(self):
        from processor.query_processor.main_graph import KBQueryWorkflow
        workflow=KBQueryWorkflow()
        calls=[]
        replacements={
            'node_item_name_confirm':lambda state:{'item_names':['test'],'rewritten_query':'test'},
            'node_search_embedding':lambda state:{'embedding_chunks':[1]},
            'node_search_embedding_hyde':lambda state:{'hyde_embedding_chunks':[2]},
            'node_web_search_mcp':lambda state:{'web_search_docs':[3]},
            'node_rerank':lambda state:{'reranked_docs':[]},
            'node_answer_output':lambda state:{'answer':'ok'},
        }
        def merge(state):
            calls.append((state['embedding_chunks'],state['hyde_embedding_chunks'],state['web_search_docs']))
            return {'rrf_chunks':[]}
        replacements['node_rrf']=merge
        with ExitStack() as stack:
            for name, fn in replacements.items():
                stack.enter_context(patch.object(getattr(workflow,name),'process',side_effect=fn))
            stack.enter_context(patch('processor.query_processor.base.require_kb_permission'))
            result=workflow.run({'session_id':'test','original_query':'test','user_id':'a','kb_id':'k'})
        self.assertEqual(result['answer'],'ok')
        self.assertEqual(calls,[([1],[2],[3])])


if __name__ == '__main__':
    unittest.main()
