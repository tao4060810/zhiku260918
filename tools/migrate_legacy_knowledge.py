"""停服后迁移明确映射的旧文档。默认预览，--apply 才写入；从不自动认领旧库。"""
import argparse
import hashlib
import json
import mimetypes
import re
import tempfile
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

from dotenv import load_dotenv
load_dotenv()
from utils.knowledge_access import require_kb_permission, kb_filter
from utils.knowledge_store import MAX_BYTES, MAX_DOCUMENTS
from utils.user_store import get_db, utcnow
from config.milvus_config import milvus_config
from config.minio_config import minio_config


def stable_id(migration_id, key):
    """
    按迁移批次和业务键生成可重复计算的 UUID
    :param migration_id: 本批迁移的唯一标识，用于重试和回退
    :param key: 知识库、文档标题或资产路径组成的业务键
    :return: 重试时保持不变的资源 ID
    """
    return str(uuid5(NAMESPACE_URL, f'zhiku:{migration_id}:{key}'))


def rows(client, collection, title):
    """
    读取指定标题的旧向量记录，拒绝重新认领已有归属的数据
    :param client: 数据库或存储客户端
    :param collection: 旧 Milvus 集合名称
    :param title: 需要迁移的文档标题
    :return: 包含完整向量字段的旧记录列表
    """
    iterator=client.query_iterator(collection_name=collection, filter=f'file_title == {json.dumps(title,ensure_ascii=False)}',
        output_fields=['*','dense_vector','sparse_vector'],batch_size=100)
    result=[]
    try:
        while batch:=iterator.next():result.extend(batch)
    finally:iterator.close()
    if any(r.get('kb_id') or r.get('user_id') for r in result):raise ValueError('旧记录已有关联账号或知识库，拒绝重新归属。')
    return result


def rewrite(value, mapping):
    """
    递归替换文本、列表和字典中的已确认资产地址
    :param value: 待转换的数据
    :param mapping: 旧地址到新私有地址的映射
    :return: 替换后的数据
    """
    if isinstance(value,str):
        for old,new in sorted(mapping.items(),key=lambda pair:-len(pair[0])):value=value.replace(old,new)
        return value
    if isinstance(value,list):return [rewrite(item,mapping) for item in value]
    if isinstance(value,dict):return {key:rewrite(item,mapping) for key,item in value.items()}
    return value


def build_plan(manifest,user_id,kb_id,migration_id,vector,storage):
    """
    校验归属、旧数据、图片映射及配额，生成迁移计划
    :param manifest: 明确指定旧集合、存储桶及文档资产映射的迁移清单
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :param migration_id: 本批迁移的唯一标识，用于重试和回退
    :param vector: Milvus 客户端
    :param storage: MinIO 客户端
    :return: 经过校验的文档迁移计划列表
    """
    # 1. 校验拥有者权限及新旧存储位置，避免把目标集合误当旧集合迁移
    require_kb_permission(user_id,kb_id,'upload')
    if manifest['legacy_chunks']==milvus_config.chunks_collection or manifest['legacy_items']==milvus_config.item_name_collection:
        raise ValueError('旧集合和新集合不能相同。')
    if manifest['legacy_bucket']==minio_config.bucket_name:raise ValueError('旧桶和私有桶不能相同。')
    plan=[];titles=set();db=get_db()
    # 2. 逐份确认文档、向量和图片映射完整，目标同名文档必须属于本批迁移
    for source in manifest['documents']:
        title=source['file_title']
        if title in titles:raise ValueError('清单中有重复文档。')
        titles.add(title)
        document_id=stable_id(migration_id,f'{kb_id}:{title}')
        existing=db.documents.find_one({'kb_id':kb_id,'file_title':title})
        if existing and (existing['_id']!=document_id or existing.get('migration_id')!=migration_id):raise ValueError('目标存在同名文档，拒绝覆盖。')
        chunks=rows(vector,manifest['legacy_chunks'],title)
        items=rows(vector,manifest['legacy_items'],title)
        if not chunks or not items:raise ValueError(f'{title}: 缺少切片或产品索引。')
        if any(not r.get('dense_vector') or not r.get('sparse_vector') for r in chunks+items):raise ValueError('旧向量不完整。')
        assets=source.get('assets',{})
        # 无法映射的 Markdown / HTML 图片必须人工明确归属，不能悄悄删除引用。
        image_urls=set()
        for chunk in chunks:
            text=chunk.get('content','')
            image_urls.update(re.findall(r'!\[[^\]]*\]\(([^\s)]+)',text))
            image_urls.update(re.findall(r'<img\b[^>]*src=[\"\']([^\"\']+)',text,re.I))
        missing=image_urls-assets.keys()
        if missing:raise ValueError(f'{title}: {len(missing)} 个图片引用尚未映射。')
        original=storage.stat_object(manifest['legacy_bucket'],source['original_key'])
        for key in assets.values():storage.stat_object(manifest['legacy_bucket'],key)
        plan.append({'document_id':document_id,'file_title':title,'source':source,'chunks':chunks,'items':items,'size_bytes':original.size})
    # 3. 将已有文档与本批计划一起检查配额，预览阶段不创建目标记录
    previous=list(db.documents.find({'kb_id':kb_id,'migration_id':{'$ne':migration_id}}))
    if len(previous)+len(plan)>MAX_DOCUMENTS or sum(d['size_bytes'] for d in previous+plan)>MAX_BYTES:raise ValueError('迁移超过知识库配额。')
    return plan


def digest_object(storage,bucket,key,target=None):
    """
    分块读取对象并计算摘要，可同时写入本地文件
    :param storage: MinIO 客户端
    :param bucket: 来源存储桶
    :param key: 对象键
    :param target: 可选的已打开二进制文件，用于接收读取内容
    :return: 对象内容的 SHA-256 摘要
    """
    response=storage.get_object(bucket,key);digest=hashlib.sha256()
    try:
        for block in response.stream(1024*1024):
            digest.update(block)
            if target:target.write(block)
    finally:response.close();response.release_conn()
    return digest.hexdigest()


def copy_asset(storage,manifest,entry,kb_id,migration_id,key,kind):
    """
    复制并校验私有资产，登记旧对象和新地址的对应关系
    :param storage: MinIO 客户端
    :param manifest: 明确指定旧集合、存储桶及文档资产映射的迁移清单
    :param entry: 当前文档的迁移计划
    :param kb_id: 本次操作所属的知识库 ID
    :param migration_id: 本批迁移的唯一标识，用于重试和回退
    :param key: 旧桶中的对象键
    :param kind: 资产类型，original 或 image
    :return: 新资产的受保护访问地址
    """
    db=get_db();asset_id=stable_id(migration_id,f'{kb_id}:{entry["document_id"]}:{kind}:{key}')
    object_key=f'knowledge/{kb_id}/documents/{entry["document_id"]}/migration/{migration_id}/{asset_id}{Path(key).suffix.lower()}'
    mime=mimetypes.guess_type(key)[0] or 'application/octet-stream'
    if kind=='image' and mime not in {'image/png','image/jpeg','image/webp','image/gif','image/bmp'}:raise ValueError('不支持的迁移图片格式。')
    # 先复制到目标私有桶，再比较内容摘要；校验通过后才登记可访问的资产记录。
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'asset'
        with path.open('wb') as target:checksum=digest_object(storage,manifest['legacy_bucket'],key,target)
        storage.fput_object(minio_config.bucket_name,object_key,str(path),content_type=mime)
        if digest_object(storage,minio_config.bucket_name,object_key)!=checksum:raise RuntimeError('复制后校验失败，未发布。')
        db.assets.replace_one({'_id':asset_id},{'_id':asset_id,'kb_id':kb_id,'document_id':entry['document_id'],
            'task_id':migration_id,'migration_id':migration_id,'kind':kind,'bucket':minio_config.bucket_name,
            'object_key':object_key,'legacy_bucket':manifest['legacy_bucket'],'legacy_object_key':key,
            'sha256':checksum,'filename':Path(key).name,'size_bytes':path.stat().st_size,'content_type':mime,'created_at':utcnow()},upsert=True)
    return '/assets/'+asset_id


def apply_plan(manifest,plan,user_id,kb_id,migration_id,vector,storage):
    """
    停服后执行迁移：关闭旧桶匿名访问，复制校验后发布新版本
    :param manifest: 明确指定旧集合、存储桶及文档资产映射的迁移清单
    :param plan: 已通过 build_plan 校验的文档计划列表
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :param migration_id: 本批迁移的唯一标识，用于重试和回退
    :param vector: Milvus 客户端
    :param storage: MinIO 客户端
    """
    from processor.import_processor.nodes.node_import_milvus import NodeImportMilvus
    from processor.import_processor.nodes.node_item_name_recognition import NodeItemNameRecognition
    from utils.milvus_utils import validate_private_schema
    db=get_db()
    journal=db.migrations.find_one({'_id':migration_id})
    # 1. 迁移 ID 绑定清单摘要和账号归属，重试不能换用另一份清单
    signature=hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if journal and (journal['kb_id']!=kb_id or journal['user_id']!=user_id or journal['manifest_hash']!=signature):raise ValueError('迁移 ID 已用于其他清单/账号。')
    db.migrations.update_one({'_id':migration_id},{'$setOnInsert':{'kb_id':kb_id,'user_id':user_id,
        'manifest_hash':signature,'manifest':manifest,'created_at':utcnow(),'state':'copying'}},upsert=True)
    # 2. 先保存策略，再关闭旧桶匿名权限；失败保持关闭，不为回退重新公开
    from minio.error import S3Error
    try:policy=storage.get_bucket_policy(manifest['legacy_bucket'])
    except S3Error as exc:
        if exc.code!='NoSuchBucketPolicy':raise
        policy=''
    if not journal:db.migrations.update_one({'_id':migration_id},{'$set':{'old_bucket_policy':policy}})
    storage.set_bucket_policy(manifest['legacy_bucket'],'')
    try:
        if storage.get_bucket_policy(manifest['legacy_bucket']):raise RuntimeError('旧桶仍有匿名策略。')
    except S3Error as exc:
        if exc.code!='NoSuchBucketPolicy':raise
    # 3. 逐份复制资产、改写引用并写入带归属字段的向量，已发布文档跳过重试
    for entry in plan:
        doc=db.documents.find_one({'_id':entry['document_id'],'kb_id':kb_id})
        if doc and doc.get('active_task')==migration_id:continue
        if doc and doc.get('active_task'):raise ValueError('迁移后的文档已更新，拒绝覆盖。')
        require_kb_permission(user_id,kb_id,'upload')
        mapping={url:copy_asset(storage,manifest,entry,kb_id,migration_id,key,'image') for url,key in entry['source'].get('assets',{}).items()}
        original=copy_asset(storage,manifest,entry,kb_id,migration_id,entry['source']['original_key'],'original')
        chunks=rewrite(entry['chunks'],mapping);items=rewrite(entry['items'],mapping)
        for collection,data,node,primary in [(milvus_config.chunks_collection,chunks,NodeImportMilvus(),'chunk_id'),
                                             (milvus_config.item_name_collection,items,NodeItemNameRecognition(),'pk')]:
            if not vector.has_collection(collection):
                if primary=='chunk_id':node._create_chunks_collection(collection,vector,len(data[0]['dense_vector']))
                else:node._create_item_name_collection(collection,vector)
            validate_private_schema(vector,collection)
            scope=kb_filter(kb_id,document_id=entry['document_id'])+f' and import_task == {json.dumps(migration_id)}'
            vector.delete(collection_name=collection,filter=scope)
            old_ids=[]
            for row in data:
                old_ids.append(row.pop(primary,None));row.pop('pk',None)
                row.update(kb_id=kb_id,document_id=entry['document_id'],import_task=migration_id)
            ids=vector.insert(collection_name=collection,data=data)['ids']
            vector.flush(collection_name=collection)
            count=vector.query(collection_name=collection,filter=scope,output_fields=['count(*)'],consistency_level='Strong')[0]['count(*)']
            if count!=len(data):raise RuntimeError('向量计数校验失败，未发布。')
            if primary=='chunk_id':
                for old,new in zip(old_ids,ids):
                    key=f'{migration_id}:{old}'
                    db.migration_chunks.replace_one({'_id':key},{'_id':key,'migration_id':migration_id,
                        'legacy_chunk_id':str(old),'chunk_id':new,'kb_id':kb_id,'document_id':entry['document_id']},upsert=True)
        for old,new in mapping.items():
            db.migration_assets.replace_one({'_id':stable_id(migration_id,entry['document_id']+old)},{'_id':stable_id(migration_id,entry['document_id']+old),
                'migration_id':migration_id,'kb_id':kb_id,'document_id':entry['document_id'],'old_url':old,'new_url':new},upsert=True)
        # 4. 资产摘要与向量数量均校验通过后，才将文档版本发布给检索使用
        db.documents.replace_one({'_id':entry['document_id']},{'_id':entry['document_id'],'kb_id':kb_id,
            'file_title':entry['file_title'],'filename':Path(entry['source']['original_key']).name,'size_bytes':entry['size_bytes'],
            'uploaded_by':user_id,'status':'ready','active_task':migration_id,'migration_id':migration_id,
            'created_at':utcnow(),'updated_at':utcnow()},upsert=True)
    db.migrations.update_one({'_id':migration_id},{'$set':{'state':'complete','completed_at':utcnow()}})


def rollback(migration_id,user_id,kb_id,vector,storage,apply=False):
    """
    预览或删除本批迁移产物，保留旧数据且不恢复匿名访问
    :param migration_id: 本批迁移的唯一标识，用于重试和回退
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :param vector: Milvus 客户端
    :param storage: MinIO 客户端
    :param apply: 是否实际写入；False 时仅返回预览结果
    """
    require_kb_permission(user_id,kb_id,'upload');db=get_db()
    docs=list(db.documents.find({'migration_id':migration_id,'kb_id':kb_id}))
    # 回退前拒绝仍被聊天引用或已经更新的迁移文档，避免破坏现有内容。
    if db.chat_message.find_one({'kb_id':kb_id}):raise ValueError('目标库已有聊天，先回退迁移聊天并处理新聊天；拒绝破坏引用。')
    if any(d.get('active_task')!=migration_id or d.get('pending_task') for d in docs):raise ValueError('迁移文档已更新，不能回退。')
    print('待回退文档:',len(docs))
    if not apply:return
    for name in (milvus_config.chunks_collection,milvus_config.item_name_collection):
        if vector.has_collection(name):vector.delete(collection_name=name,filter=kb_filter(kb_id)+f' and import_task == {json.dumps(migration_id)}')
    query={'migration_id':migration_id,'kb_id':kb_id}
    for asset in db.assets.find(query):storage.remove_object(asset['bucket'],asset['object_key'])
    for name in ('assets','documents','migration_chunks','migration_assets'):db[name].delete_many(query)
    db.migrations.update_one({'_id':migration_id,'kb_id':kb_id},{'$set':{'state':'rolled_back'}})


def main():
    """
    解析迁移清单和执行选项，默认预览，仅 --apply 时写入
    """
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--user-id',required=True);parser.add_argument('--kb-id',required=True)
    parser.add_argument('--migration-id',required=True,type=lambda s:str(__import__('uuid').UUID(s)))
    parser.add_argument('--apply',action='store_true');parser.add_argument('--rollback',action='store_true')
    parser.add_argument('--close-legacy-public',action='store_true',help='应用时明确同意关闭整个旧桶的匿名策略')
    args=parser.parse_args()
    from utils.milvus_utils import get_milvus_client
    from utils.minio_utils import get_minio_client
    from minio import Minio
    import os
    # 预览不创建新桶、不修改策略。
    storage=Minio(minio_config.endpoint,access_key=minio_config.access_key,
                  secret_key=minio_config.secret_key,secure=os.getenv('MINIO_SECURE','false').lower()=='true')
    vector=get_milvus_client()
    if args.rollback:
        rollback(args.migration_id,args.user_id,args.kb_id,vector,storage,args.apply);return
    manifest=json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    plan=build_plan(manifest,args.user_id,args.kb_id,args.migration_id,vector,storage)
    print(json.dumps([{'title':p['file_title'],'chunks':len(p['chunks']),'items':len(p['items']),'images':len(p['source'].get('assets',{})),'bytes':p['size_bytes']} for p in plan],ensure_ascii=False,indent=2))
    if args.apply:
        if not args.close_legacy_public:parser.error('--apply 需要 --close-legacy-public；先停服并备份。')
        storage=get_minio_client()
        apply_plan(manifest,plan,args.user_id,args.kb_id,args.migration_id,vector,storage)
        print('迁移完成；旧数据保留，旧桶匿名访问保持关闭。')


if __name__=='__main__':main()
