"""迁移回归：用存储和向量替身验证预览、重复执行、发布与回退。"""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch
import io
import re

from support import DatabaseCase
from tools import migrate_legacy_chats as chats
from tools import migrate_legacy_knowledge as knowledge
from config.milvus_config import milvus_config
from config.minio_config import minio_config


class ChatMigrationTests(DatabaseCase):
    def setUp(self):
        super().setUp();self.user,self.kb=self.account();self.migration=str(uuid4())

    def test_preview_apply_repeat_and_conditional_rollback(self):
        original={'session_id':'legacy','role':'user','text':'old question','ts':1}
        self.db.chat_message.insert_one(original)
        result=chats.migrate(self.user['_id'],self.kb['_id'],self.migration)
        self.assertEqual(result['messages'],1);self.assertNotIn('user_id',self.db.chat_message.find_one())
        chats.migrate(self.user['_id'],self.kb['_id'],self.migration,True)
        self.assertEqual(self.db.chat_message.find_one()['text'],'old question')
        self.assertEqual(chats.migrate(self.user['_id'],self.kb['_id'],self.migration,True)['messages'],0)
        self.db.chat_message.update_one({'_id':original['_id']},{'$set':{'text':'new edit'}})
        with self.assertRaises(ValueError):chats.rollback(self.user['_id'],self.kb['_id'],self.migration,True)
        self.db.chat_message.update_one({'_id':original['_id']},{'$set':{'text':'old question'}})
        chats.rollback(self.user['_id'],self.kb['_id'],self.migration,True)
        self.assertEqual(self.db.chat_message.find_one(),original)

    def test_unmapped_citation_quarantines_whole_chat(self):
        self.db.chat_message.insert_many([{'session_id':'x','role':'user','text':'q'},
            {'session_id':'x','role':'assistant','text':'a[cite:1]','sources':[{'chunk_id':12,'source':'local'}]}])
        report=chats.migrate(self.user['_id'],self.kb['_id'],self.migration,True)
        self.assertEqual(report['messages'],0);self.assertEqual(len(report['quarantined']),1)
        self.assertEqual(self.db.chat_message.count_documents({'user_id':{'$exists':True}}),0)

    def test_interrupted_chat_resumes_from_journal(self):
        self.db.chat_message.insert_one({'session_id':'legacy','role':'user','text':'q'})
        with patch.object(chats,'apply_chat',side_effect=RuntimeError('interruption')):
            with self.assertRaises(RuntimeError):chats.migrate(self.user['_id'],self.kb['_id'],self.migration,True)
        chats.migrate(self.user['_id'],self.kb['_id'],self.migration,True)
        self.assertEqual(self.db.chat_sessions.count_documents({'user_id':self.user['_id']}),1)
        self.assertEqual(self.db.chat_message.count_documents({'user_id':self.user['_id']}),1)


class VectorFake:
    def __init__(self):
        vectors={'dense_vector':[.1,.2],'sparse_vector':{1:.5}}
        self.data={'legacy_chunks':[{'chunk_id':1,'file_title':'manual','content':'Fact ![picture](http://old/manual/p.png)',**vectors}],
            'legacy_items':[{'pk':2,'file_title':'manual','item_name':'product',**vectors}],
            milvus_config.chunks_collection:[],milvus_config.item_name_collection:[]}
        self.calls=0
    def query_iterator(self,collection_name,filter,**kwargs):
        matches=deepcopy([r for r in self.data[collection_name] if r['file_title']=='manual'])
        batches=iter([matches,[]]);return SimpleNamespace(next=lambda:next(batches),close=lambda:None)
    def has_collection(self,name):return name in self.data
    def describe_collection(self,**kwargs):return {'fields':[{'name':n} for n in ['kb_id','document_id','import_task']]}
    def match(self,row,expr):return all(row.get(key)==value for key,value in re.findall(r'(kb_id|document_id|import_task) == "([^"]+)"',expr))
    def delete(self,collection_name,filter):self.data[collection_name]=[r for r in self.data[collection_name] if not self.match(r,filter)]
    def insert(self,collection_name,data):
        self.calls+=1;ids=[]
        for row in data:
            row=deepcopy(row);key='chunk_id' if collection_name==milvus_config.chunks_collection else 'pk'
            row[key]=self.calls*100+len(ids);ids.append(row[key]);self.data[collection_name].append(row)
        return {'ids':ids}
    def flush(self,**kwargs):pass
    def query(self,collection_name,filter,**kwargs):return [{'count(*)':sum(self.match(r,filter) for r in self.data[collection_name])}]


class StorageFake:
    def __init__(self):
        self.objects={('legacy','manual.md'):b'# original',('legacy','p.png'):b'image'}
        self.policy='public';self.writes=0
    def stat_object(self,bucket,key):return SimpleNamespace(size=len(self.objects[bucket,key]))
    def get_object(self,bucket,key):return SimpleNamespace(stream=lambda size:iter([self.objects[bucket,key]]),close=lambda:None,release_conn=lambda:None)
    def fput_object(self,bucket,key,path,**kwargs):
        from pathlib import Path
        self.writes+=1;self.objects[bucket,key]=Path(path).read_bytes()
    def remove_object(self,bucket,key):del self.objects[bucket,key]
    def get_bucket_policy(self,bucket):return self.policy
    def set_bucket_policy(self,bucket,policy):self.policy=policy


class KnowledgeMigrationTests(DatabaseCase):
    def setUp(self):
        super().setUp();self.user,self.kb=self.account();self.migration=str(uuid4())
        self.vector=VectorFake();self.storage=StorageFake()
        self.manifest={'legacy_chunks':'legacy_chunks','legacy_items':'legacy_items','legacy_bucket':'legacy',
            'documents':[{'file_title':'manual','original_key':'manual.md','assets':{'http://old/manual/p.png':'p.png'}}]}

    def plan(self):return knowledge.build_plan(self.manifest,self.user['_id'],self.kb['_id'],self.migration,self.vector,self.storage)
    def apply(self):knowledge.apply_plan(self.manifest,self.plan(),self.user['_id'],self.kb['_id'],self.migration,self.vector,self.storage)

    def test_preview_no_writes_apply_scoped_idempotent_and_rollback(self):
        self.assertEqual(len(self.plan()),1);self.assertEqual(self.storage.writes,0);self.assertEqual(self.db.documents.count_documents({}),0)
        self.apply();self.assertEqual(self.storage.policy,'')
        doc=self.db.documents.find_one();self.assertEqual(doc['kb_id'],self.kb['_id'])
        row=self.vector.data[milvus_config.chunks_collection][0]
        self.assertEqual(row['document_id'],doc['_id']);self.assertNotIn('http://old',row['content'])
        self.assertIn('/assets/',row['content']);self.assertEqual(self.db.assets.count_documents({}),2)
        calls=self.vector.calls;self.apply();self.assertEqual(self.vector.calls,calls)
        knowledge.rollback(self.migration,self.user['_id'],self.kb['_id'],self.vector,self.storage,True)
        self.assertEqual(self.db.documents.count_documents({}),0);self.assertEqual(self.storage.policy,'')
        self.assertEqual(len(self.vector.data['legacy_chunks']),1)

    def test_missing_mapping_and_owned_source_are_rejected(self):
        self.manifest['documents'][0]['assets']={}
        with self.assertRaises(ValueError):self.plan()
        self.manifest['documents'][0]['assets']={'http://old/manual/p.png':'p.png'}
        self.vector.data['legacy_chunks'][0]['kb_id']='another'
        with self.assertRaises(ValueError):self.plan()
        self.assertEqual(self.storage.writes,0)

    def test_failed_copy_not_published_then_resume(self):
        original=self.storage.fput_object
        def corrupt(bucket,key,path,**kwargs):original(bucket,key,path,**kwargs);self.storage.objects[bucket,key]=b'corrupt'
        with patch.object(self.storage,'fput_object',side_effect=corrupt):
            with self.assertRaises(RuntimeError):self.apply()
        self.assertEqual(self.db.documents.count_documents({}),0)
        self.apply();self.assertEqual(self.db.documents.count_documents({}),1)

    def test_chat_references_preserve_semantics_after_knowledge_migration(self):
        self.apply()
        message={'role':'assistant','text':'Fact[cite:1]\n【图片】\nhttp://old/manual/p.png',
            'sources':[{'source_id':'1','source':'local','chunk_id':1,'content':'Fact ![picture](http://old/manual/p.png)'}],
            'image_urls':['http://old/manual/p.png']}
        changed=chats.convert_message(message,self.kb['_id'])
        self.assertTrue(changed['text'].startswith('Fact[cite:1]'));self.assertNotIn('http://old',str(changed))
        self.assertEqual(changed['sources'][0]['document_id'],self.db.documents.find_one()['_id'])
