"""运行真实处理图并替换外部服务，验证切片、产品和回答引用的知识库归属。"""
from contextlib import ExitStack
from unittest.mock import patch, MagicMock
from uuid import uuid4

from support import DatabaseCase
from test_migrations import VectorFake
from utils.knowledge_access import AccessDenied
from utils import task_utils as tasks
from web.api.workflows import run_import_graph
from config.milvus_config import milvus_config


def embeddings(texts):return {'dense':[[.1,.2] for _ in texts],'sparse':[{1:.5} for _ in texts]}


class KnowledgeTests(DatabaseCase):
    def setUp(self):
        super().setUp();self.user,self.kb=self.account();self.login()

    def test_real_clarification_graph_saves_assistant_once(self):
        path='processor.query_processor.nodes.node_item_name_confirm.NodeItemNameConfirm._step_4_extract_info'
        with patch(path,return_value={'item_names':[],'rewritten_query':'q'}):
            response=self.client.post('/query',json={'query':'q','kb_id':self.kb['_id'],'is_stream':False})
        self.assertEqual(response.status_code,200,response.text)
        scope={'user_id':self.user['_id'],'session_id':response.json()['session_id']}
        self.assertEqual(self.db.chat_message.count_documents({**scope,'role':'assistant'}),1)
        self.assertEqual(self.db.chat_message.count_documents({**scope,'role':'user'}),1)

    def test_real_markdown_graph_scopes_products_and_chunks(self):
        # 真正执行 LangGraph 及解析/切片/写入节点，只替换模型和外部存储边界。
        vector=VectorFake()
        modules='processor.import_processor.nodes.'
        with ExitStack() as stack:
            stack.enter_context(patch('web.api.workflows.store_asset',return_value='/assets/test'))
            stack.enter_context(patch('utils.milvus_utils.cleanup_import_vectors'))
            stack.enter_context(patch(modules+'node_item_name_recognition.NodeItemNameRecognition._step_3_call_llm',return_value='same product'))
            for name in ('node_item_name_recognition','node_bge_embedding'):
                stack.enter_context(patch(modules+name+'.generate_embeddings',side_effect=embeddings))
            for name in ('node_item_name_recognition','node_import_milvus'):
                stack.enter_context(patch(modules+name+'.get_milvus_client',return_value=vector))
            response=self.client.post('/upload',params={'kb_id':self.kb['_id']},files={'files':('manual.md',b'# Test\nA personal instruction manual.\n')})
            self.assertEqual(response.status_code,202)
            from web.app import app
            app.state.executor.submit(lambda:None).result(timeout=15)
        task=tasks.get_task(response.json()['task_ids'][0])
        self.assertEqual(task['status'],'completed',task['error'])
        self.assertGreater(task['result']['chunk_count'],0)
        for name in (milvus_config.chunks_collection,milvus_config.item_name_collection):
            for row in vector.data[name]:
                self.assertEqual(row['kb_id'],self.kb['_id']);self.assertEqual(row['document_id'],task['document_id'])
                self.assertEqual(row['import_task'],task['task_id'])
        self.assertEqual(self.db.documents.find_one()['active_task'],task['task_id'])

    def test_every_search_path_forces_kb_and_published_version(self):
        from processor.query_processor.nodes.node_search_embedding import NodeSearchEmbedding
        from processor.query_processor.nodes.node_search_embedding_hyde import NodeSearchEmbeddingHyde
        from processor.query_processor.nodes.node_item_name_confirm import NodeItemNameConfirm
        version=str(uuid4());doc=str(uuid4())
        self.db.documents.insert_one({'_id':doc,'kb_id':self.kb['_id'],'file_title':'manual','active_task':version})
        for name,invoke in [
            ('node_search_embedding',lambda:NodeSearchEmbedding().process({'kb_id':self.kb['_id'],'rewritten_query':'q','item_names':[]})),
            ('node_search_embedding_hyde',lambda:NodeSearchEmbeddingHyde()._step_2_search_embedding_hyde('q','hypothesis',kb_id=self.kb['_id'])),
            ('node_item_name_confirm',lambda:NodeItemNameConfirm()._step_5_vectorize_and_query(['product'],self.kb['_id'])),
        ]:
            prefix='processor.query_processor.nodes.'+name
            with self.subTest(path=name),patch(prefix+'.generate_embeddings',side_effect=embeddings),patch(prefix+'.get_milvus_client'),patch(prefix+'.create_hybrid_search_requests') as request,patch(prefix+'.hybrid_search',return_value=[[]]):
                invoke();expr=request.call_args.kwargs['expr']
                self.assertIn(self.kb['_id'],expr);self.assertIn(version,expr);self.assertIn('import_task in',expr)

    def test_foreign_result_rejected_before_prompt_or_reranker(self):
        from processor.query_processor.nodes.node_answer_output import NodeAnswerOutput
        from processor.query_processor.nodes.node_rerank import NodeRerank
        alien={'kb_id':str(uuid4()),'document_id':str(uuid4()),'source':'local','content':'other user secret'}
        state={'user_id':self.user['_id'],'kb_id':self.kb['_id'],'reranked_docs':[alien],'rrf_chunks':[alien]}
        with self.assertRaises(AccessDenied):NodeAnswerOutput()._step_2_construct_prompt(state)
        with self.assertRaises(AccessDenied):NodeRerank()._step_1_merge_multi_source_docs(state)

    def test_import_failure_releases_new_document_and_removes_only_new_assets(self):
        with patch('web.api.workflows.store_asset',side_effect=RuntimeError('offline')),patch('web.api.workflows.clean_failed_assets') as cleanup,patch('utils.milvus_utils.cleanup_import_vectors'):
            response=self.client.post('/upload',params={'kb_id':self.kb['_id']},files={'files':('manual.md',b'# example')})
            from web.app import app
            app.state.executor.submit(lambda:None).result(timeout=10)
        task=tasks.get_task(response.json()['task_ids'][0])
        self.assertEqual(task['status'],'failed');self.assertEqual(self.db.documents.count_documents({}),0)
        self.assertEqual(cleanup.call_args.args[0]['user_id'],self.user['_id'])

    def test_canceled_import_never_publishes_and_cleans_its_files(self):
        from web.app import app
        from pathlib import Path
        for when in ('queued', 'backup', 'before_publish'):
            with self.subTest(when=when):
                with patch('web.api.import_service.run_import_graph'):
                    response=self.client.post('/upload',params={'kb_id':self.kb['_id']},
                                              files={'files':('manual.md',b'# canceled')})
                    app.state.executor.submit(lambda:None).result(timeout=5)
                task_id=response.json()['task_ids'][0]
                task=tasks.get_task(task_id)
                path=self.root / task['kb_id'] / task['document_id'] / task_id / 'manual.md'
                if when=='queued':
                    tasks.cancel_task(task_id)
                    task=tasks.get_task(task_id)
                def backup(*args):
                    if when=='backup':
                        tasks.cancel_task(task_id)
                def invoke(state):
                    tasks.cancel_task(task_id)
                    return {'chunks':['unpublished']}
                with patch('web.api.workflows.store_asset',side_effect=backup) as store, \
                     patch('processor.import_processor.main_graph.KBImportWorkflow') as workflow, \
                     patch('web.api.workflows.clean_failed_assets') as assets, \
                     patch('utils.milvus_utils.cleanup_import_vectors') as vectors:
                    workflow.return_value.graph.invoke.side_effect=invoke
                    run_import_graph(task_id,path)
                self.assertEqual(tasks.get_task(task_id)['status'],'canceled')
                self.assertEqual(self.db.documents.count_documents({}),0)
                self.assertFalse(Path(path).parent.exists())
                assets.assert_called_once_with(task)
                vectors.assert_called_once_with(task)
                if when=='queued':
                    store.assert_not_called()
                if when!='before_publish':
                    workflow.assert_not_called()
