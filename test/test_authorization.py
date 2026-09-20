"""权限回归：用户间任务、会话、资产隔离，以及 SSE 期间的权限复查。"""
from concurrent.futures import Future
from unittest.mock import patch, MagicMock, AsyncMock
from types import SimpleNamespace
from uuid import uuid4
import asyncio

from support import DatabaseCase
from utils import task_utils as tasks
from utils.knowledge_access import AccessDenied, require_kb_permission, search_filter, check_local_docs
from utils.knowledge_store import reserve_documents, finish_document, mutation_lock, recover_interrupted_imports
from utils.auth_utils import LoginRequired
from utils.sse_utils import sse_generator
from utils.asset_references import sanitize_sources
from utils.mongo_history_utils import save_chat_message, get_recent_messages


class AuthorizationTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.a,self.ka=self.account('alice','admin');self.b,self.kb=self.account('bob')
        self.login('alice')
        self.sa=self.chat(self.a,self.ka);self.sb=self.chat(self.b,self.kb)

    def test_cross_user_all_read_write_paths_including_admin(self):
        task=tasks.create_task('query',user_id=self.b['_id'],kb_id=self.kb['_id'],session_id=self.sb)
        for path in [f'/history/{self.sb}',f'/status/{task}',f'/stream/{self.sb}?task_id={task}',
                     f'/knowledge-bases/{self.kb["_id"]}/documents',f'/knowledge-bases/{self.kb["_id"]}/status',f'/tasks?kb_id={self.kb["_id"]}']:
            self.assertEqual(self.client.get(path).status_code,404,path)
        self.assertEqual(self.client.delete(f'/history/{self.sb}').status_code,404)
        self.assertEqual(self.client.post('/query',json={'query':'secret','kb_id':self.ka['_id'],'session_id':self.sb}).status_code,404)
        self.assertEqual(self.client.post('/query',json={'query':'secret','kb_id':self.kb['_id']}).status_code,404)
        self.assertEqual(self.client.post('/upload',params={'kb_id':self.kb['_id']},files={'files':('a.md',b'x')}).status_code,404)
        self.assertEqual(self.client.get('/tasks').json()['items'],[])
        self.assertFalse(list(self.root.rglob('*.md')))
        self.db.knowledge_members.insert_one({'kb_id':self.kb['_id'],'user_id':self.a['_id'],'role':'reader'})
        with self.assertRaises(AccessDenied):require_kb_permission(self.a['_id'],self.kb['_id'])

    def test_forged_identity_and_message_id_cannot_write_others(self):
        response=self.client.post('/query',json={'query':'x','kb_id':self.ka['_id'],'user_id':self.b['_id']})
        self.assertEqual(response.status_code,422)
        message=save_chat_message(self.sb,'user','private',user_id=self.b['_id'],kb_id=self.kb['_id'])
        with self.assertRaises(AccessDenied):save_chat_message(self.sa,'user','overwrite',message_id=message,user_id=self.a['_id'],kb_id=self.ka['_id'])
        with self.assertRaises(AccessDenied):get_recent_messages(self.sb,user_id=self.a['_id'],kb_id=self.ka['_id'])
        self.assertEqual(self.client.get('/sessions').json()['items'],[])

    def test_delete_active_chat_rejected(self):
        task=tasks.create_task('query',user_id=self.a['_id'],kb_id=self.ka['_id'],session_id=self.sa)
        self.assertEqual(self.client.delete('/history/'+self.sa).status_code,409)
        tasks.finish_task(task,result={'answer':'done'})
        self.assertEqual(self.client.delete('/history/'+self.sa).status_code,200)
        with self.assertRaises(AccessDenied):save_chat_message(self.sa,'assistant','late',user_id=self.a['_id'],kb_id=self.ka['_id'])

    def test_upload_same_names_isolate_and_failure_releases_reservation(self):
        with patch('web.api.import_service.run_import_graph'):
            first=self.client.post('/upload',params={'kb_id':self.ka['_id']},files={'files':('same.md',b'alice')})
            self.assertEqual(first.status_code,202)
            self.login('bob')
            second=self.client.post('/upload',params={'kb_id':self.kb['_id']},files={'files':('same.md',b'bob')})
            self.assertEqual(second.status_code,202)
        a=tasks.get_task(first.json()['task_ids'][0]);b=tasks.get_task(second.json()['task_ids'][0])
        self.assertNotEqual(a['document_id'],b['document_id'])
        self.assertEqual(self.db.documents.count_documents({}),2)
        finish_document(a,False)
        self.assertEqual(self.db.documents.count_documents({'kb_id':self.ka['_id']}),0)
        self.assertEqual(self.db.documents.count_documents({'kb_id':self.kb['_id']}),1)

    def test_quota_atomic_and_duplicate_batch(self):
        for files in ([('files',('a.md',b'a')),('files',('a.pdf',b'%PDF-1.7'))],
                      [('files',('a.md',b'a')),('files',('b.md',b'b'))]):
            with patch('utils.knowledge_store.MAX_DOCUMENTS',1),patch('web.api.import_service.run_import_graph') as run:
                response=self.client.post('/upload',params={'kb_id':self.ka['_id']},files=files)
                self.assertEqual(response.status_code,409);run.assert_not_called()
            self.assertEqual(self.db.documents.count_documents({}),0)

    def test_only_published_generation_searchable_after_failure_restart(self):
        doc=str(uuid4());old=str(uuid4());new=str(uuid4())
        self.db.documents.insert_one({'_id':doc,'kb_id':self.ka['_id'],'file_title':'x','size_bytes':10,
            'active_task':old,'pending_task':new,'pending_size':20,'status':'pending'})
        expr=search_filter(self.ka['_id'],item_names=['a" or kb_id != "'])
        self.assertIn(self.ka['_id'],expr);self.assertIn(old,expr);self.assertNotIn(new,expr)
        recover_interrupted_imports()
        self.assertIn(old,search_filter(self.ka['_id']))
        self.assertNotIn('pending_size',self.db.documents.find_one({'_id':doc}))
        with self.assertRaises(AccessDenied):check_local_docs([{'entity':{'kb_id':self.kb['_id'],'document_id':doc}}],self.ka['_id'],hits=True)

    def test_assets_private_and_cross_document_images_removed(self):
        asset=str(uuid4());doc=str(uuid4())
        self.db.assets.insert_one({'_id':asset,'kb_id':self.kb['_id'],'document_id':doc,'kind':'image',
            'bucket':'private','object_key':'hidden','filename':'x.png','content_type':'image/png'})
        with patch('web.api.asset_service.get_minio_client') as storage:
            self.assertEqual(self.client.get('/assets/'+asset).status_code,404);storage.assert_not_called()
        source={'source':'local','kb_id':self.ka['_id'],'document_id':doc,'content':f'![x](/assets/{asset}) ![e](https://evil.test/x.png)'}
        self.assertNotIn('/assets/',sanitize_sources([source],self.ka['_id'])[0]['content'])
        self.login('bob')
        response=MagicMock();response.stream.return_value=[b'image'];response.headers={'Content-Length':'5'}
        with patch('web.api.asset_service.get_minio_client') as storage:
            storage.return_value.get_object.return_value=response
            result=self.client.get('/assets/'+asset)
        self.assertEqual(result.status_code,200);self.assertEqual(result.content,b'image')
        self.assertIn('no-store',result.headers['cache-control']);response.close.assert_called_once()

    def test_sse_revalidates_before_replaying_private_events(self):
        task=tasks.create_task('query',user_id=self.a['_id'],kb_id=self.ka['_id'],session_id=self.sa)
        tasks.publish_event(task,'delta',{'delta':'secret'})
        request=SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        async def collect():
            return [m async for m in sse_generator(task,request,validate=MagicMock(side_effect=LoginRequired()))]
        output=''.join(asyncio.run(collect()))
        self.assertIn('AUTH_EXPIRED',output);self.assertNotIn('secret',output)

    def test_inline_answer_images_preserve_scope_and_history_in_both_modes(self):
        from processor.query_processor.nodes.node_answer_output import NodeAnswerOutput
        own, foreign, doc = str(uuid4()), str(uuid4()), str(uuid4())
        self.db.assets.insert_many([
            {'_id':own, 'kb_id':self.ka['_id'], 'document_id':doc, 'kind':'image'},
            {'_id':foreign, 'kb_id':self.kb['_id'], 'document_id':doc, 'kind':'image'},
        ])
        docs = [
            {'source':'local', 'kb_id':self.ka['_id'], 'document_id':doc, 'content':'配置步骤'},
            {'source':'local', 'kb_id':self.ka['_id'], 'document_id':doc,
             'content':f'![连接图](/assets/{own}) ![越权图](/assets/{foreign})'},
        ]
        answer = f'Steps[cite:1]\n\n![连接图](/assets/{own})\n\nNext.\n![越权图](/assets/{foreign})'
        expected = f'Steps[cite:1]\n\n![连接图](/assets/{own})\n\nNext.'
        llm = SimpleNamespace(invoke=lambda prompt: SimpleNamespace(content=answer),
                              stream=lambda prompt: iter([SimpleNamespace(content=answer)]))
        for streaming in (False, True):
            with self.subTest(streaming=streaming), \
                 patch('processor.query_processor.nodes.node_answer_output.get_llm_client', return_value=llm) as client:
                state = {'user_id':self.a['_id'], 'kb_id':self.ka['_id'], 'session_id':self.sa,
                         'original_query':'展示连接图', 'reranked_docs':docs, 'is_stream':streaming}
                result = NodeAnswerOutput().process(state)
            self.assertNotIn(foreign, result['prompt'])
            self.assertNotIn(foreign, result['answer'])
            client.assert_called_once_with()
            self.assertEqual(result['image_urls'], [f'/assets/{own}'])
            self.assertEqual(len(result['sources']), 2)
            history = self.client.get('/history/'+self.sa).json()['items'][-1]
            self.assertEqual(history['image_urls'], result['image_urls'])
            self.assertEqual(history['text'], expected)

    def test_blocking_query_does_not_return_after_logout(self):
        from utils.auth_utils import revoke_session
        raw=self.client.cookies.get('zhiku_auth')
        def worker(task_id,*args):
            tasks.finish_task(task_id,result={'answer':'secret'})
            revoke_session(raw)
        with patch('web.api.query_service.run_query_graph',side_effect=worker):
            response=self.client.post('/query',json={'query':'q','kb_id':self.ka['_id'],'is_stream':False})
        self.assertEqual(response.status_code,401);self.assertNotIn('secret',response.text)

    def test_private_storage_rejects_public_policy(self):
        from utils import minio_utils
        client=MagicMock();client.get_bucket_policy.return_value='{"Statement":[{"Principal":"*"}]}'
        with patch.object(minio_utils,'_client',client):
            with self.assertRaises(RuntimeError):minio_utils.get_minio_client()
        client.set_bucket_policy.assert_not_called()

    def test_user_can_cancel_own_import_but_not_other_user(self):
        own = tasks.create_task('import', user_id=self.a['_id'], kb_id=self.ka['_id'], filename='manual.md')
        foreign = tasks.create_task('import', user_id=self.b['_id'], kb_id=self.kb['_id'], filename='private.md')
        response = self.client.post(f'/tasks/{own}/cancel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'canceling')
        self.assertEqual(self.client.post(f'/tasks/{foreign}/cancel').status_code, 404)
        tasks.finish_task(own, result={'chunk_count': 99})
        self.assertEqual(tasks.get_task(own)['status'], 'canceled')
        self.assertEqual(tasks.get_task(own)['result'], {})
        self.assertEqual(self.client.get('/auth/me').status_code, 200)
        query = tasks.create_task('query', user_id=self.a['_id'], kb_id=self.ka['_id'])
        self.assertEqual(self.client.post(f'/tasks/{query}/cancel').status_code, 404)

    def test_upload_cancellation_before_acceptance_is_scoped_to_account(self):
        upload_id = str(uuid4())
        foreign = tasks.create_task('import', user_id=self.b['_id'], kb_id=self.kb['_id'], upload_id=upload_id)
        self.assertEqual(self.client.post(f'/uploads/{upload_id}/cancel').status_code, 204)
        self.assertEqual(tasks.get_task(foreign)['status'], 'pending')
        with patch('web.api.import_service.run_import_graph') as run:
            response = self.client.post('/upload', params={'kb_id':self.ka['_id'], 'upload_id':upload_id},
                                        files={'files':('manual.md', b'# canceled')})
        self.assertEqual(response.status_code, 409)
        run.assert_not_called()
        self.assertEqual(self.db.documents.count_documents({'kb_id':self.ka['_id']}), 0)
        self.assertFalse(list(self.root.rglob('*.md')))

    def test_upload_cancellation_after_acceptance_preserves_completed_tasks(self):
        upload_id = str(uuid4())
        active = tasks.create_task('import', user_id=self.a['_id'], kb_id=self.ka['_id'], upload_id=upload_id)
        done = tasks.create_task('import', user_id=self.a['_id'], kb_id=self.ka['_id'], upload_id=upload_id)
        tasks.finish_task(done, result={'chunk_count': 1})
        self.assertEqual(self.client.post(f'/uploads/{upload_id}/cancel').status_code, 204)
        self.assertEqual(tasks.get_task(active)['status'], 'canceling')
        self.assertEqual(tasks.get_task(done)['status'], 'completed')
        self.assertEqual(tasks.get_task(done)['result'], {'chunk_count':1})

    def test_invalid_asset_html_and_bare_references_do_not_reach_prompt(self):
        asset=str(uuid4());doc=str(uuid4())
        source={'source':'local','kb_id':self.ka['_id'],'document_id':doc,
            'content':f'<img src="/assets/{asset}"> /assets/{asset} <img src="https://evil.test/x">'}
        sanitized=sanitize_sources([source],self.ka['_id'])[0]['content']
        self.assertNotIn('/assets/',sanitized);self.assertNotIn('evil.test',sanitized)
