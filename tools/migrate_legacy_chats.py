"""将可确定归属的旧聊天迁给指定用户；未映射引用整段隔离，默认只预览。"""
import argparse
import hashlib
from copy import deepcopy
from uuid import UUID, uuid5, NAMESPACE_URL
from bson import json_util
from dotenv import load_dotenv
load_dotenv()

from utils.knowledge_access import require_kb_permission
from utils.user_store import get_db, utcnow
from tools.migrate_legacy_knowledge import rewrite


def fingerprint(record):
    """
    计算旧消息快照的稳定摘要
    :param record: 待计算摘要的消息记录
    :return: 用于比较记录内容的 SHA-256 摘要
    """
    return hashlib.sha256(json_util.dumps(record,sort_keys=True).encode()).hexdigest()


def convert_message(message,kb_id):
    """
    保留消息内容，仅按已记录的迁移映射转换来源和图片地址
    :param message: 待迁移的旧消息记录
    :param kb_id: 目标知识库 ID
    :return: 转换后的消息副本；来源或图片无法唯一映射时抛出异常
    """
    db=get_db();result=deepcopy(message);mapping={}
    # 1. 本地引用必须唯一映射到已成功迁移且尚未被替换的文档版本
    for source in result.get('sources') or []:
        if source.get('source')=='web':continue
        matches=list(db.migration_chunks.find({'kb_id':kb_id,'legacy_chunk_id':str(source.get('chunk_id'))}))
        if len(matches)!=1:raise ValueError('本地来源切片无法唯一映射')
        found=matches[0]
        if not db.documents.find_one({'_id':found['document_id'],'kb_id':kb_id,'active_task':found['migration_id']}):raise ValueError('来源文档尚未成功迁移或已经被替换')
        source.update(source='local',kb_id=kb_id,document_id=found['document_id'],chunk_id=found['chunk_id'])
        for asset in db.migration_assets.find({'kb_id':kb_id,'document_id':found['document_id']}):
            if asset['old_url'] in mapping and mapping[asset['old_url']]!=asset['new_url']:raise ValueError('同一图片引用跨文档，需人工明确映射')
            mapping[asset['old_url']]=asset['new_url']
    # 2. 检查消息附件和助手正文中的图片，未映射图片会阻止整段会话迁移
    for url in result.get('image_urls') or []:
        if url not in mapping:raise ValueError('历史图片缺少迁移映射')
    if result.get('role')=='assistant':
        import re
        urls=re.findall(r'!\[[^\]]*\]\(([^\s)]+)',result.get('text',''))
        if '【图片】' in result.get('text',''):urls+=re.findall(r'https?://[^\s<>]+',result['text'].split('【图片】',1)[1])
        if any(url not in mapping for url in urls):raise ValueError('答案图片链接不能映射')
        result=rewrite(result,mapping)
    return result


def plan_chats(user_id,kb_id,migration_id):
    """
    生成可迁移会话计划，并列出无法确定归属或来源的会话
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :param migration_id: 本批迁移的唯一标识，用于重试和回退
    :return: 迁移计划列表和待人工核对的隔离记录列表
    """
    require_kb_permission(user_id,kb_id);db=get_db();plans=[];quarantine=[]
    # 仅将完全没有归属字段的记录列为候选；同一会话内存在混合归属时隔离整段。
    legacy=list(db.chat_message.find({'user_id':{'$exists':False},'kb_id':{'$exists':False}}))
    sessions={row.get('session_id') for row in legacy}
    for session_id in sessions:
        records=list(db.chat_message.find({'session_id':session_id}).sort([('ts',1),('_id',1)]))
        if not session_id or any('user_id' in r or 'kb_id' in r for r in records):
            quarantine.append({'session_id':session_id,'reason':'归属冲突或缺少会话 ID'});continue
        try:
            converted=[convert_message(row,kb_id) for row in records]
            new_session=str(uuid5(NAMESPACE_URL,f'zhiku-chat:{migration_id}:{session_id}'))
            existing=db.chat_sessions.find_one({'_id':new_session})
            if existing and (existing.get('migration_id')!=migration_id or existing.get('user_id')!=user_id or existing.get('kb_id')!=kb_id):raise ValueError('目标会话归属冲突')
            plans.append({'old_session':session_id,'session_id':new_session,'original':records,'converted':converted})
        except ValueError as exc:quarantine.append({'session_id':session_id,'reason':str(exc)})
    return plans,quarantine


def migrate(user_id,kb_id,migration_id,apply=False):
    """
    预览或执行旧会话迁移，执行时可继续未完成的计划
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :param migration_id: 本批迁移的唯一标识，用于重试和回退
    :param apply: 是否实际写入；False 时仅返回预览结果
    :return: 可迁移会话数、消息数和隔离原因
    """
    db=get_db();journal=db.chat_migrations.find_one({'_id':migration_id})
    if journal and (journal['user_id']!=user_id or journal['kb_id']!=kb_id):raise ValueError('迁移 ID 已用于其他归属')
    # 中断后先恢复未完成会话，仍按原记录快照及条件更新，绝不重新认领其他账号记录。
    if apply and journal:
        for plan in db.chat_migration_plans.find({'migration_id':migration_id,'state':{'$ne':'complete'}}):apply_chat(plan,user_id,kb_id,migration_id)
    plans,quarantine=plan_chats(user_id,kb_id,migration_id)
    report={'chats':len(plans),'messages':sum(len(p['original']) for p in plans),'quarantined':quarantine}
    # 预览到此结束，不写入迁移日志、消息或会话记录。
    if not apply:return report
    db.chat_migrations.update_one({'_id':migration_id},{'$setOnInsert':{'user_id':user_id,'kb_id':kb_id,'created_at':utcnow()}},upsert=True)
    for plan in plans:
        plan.update(_id=f'{migration_id}:{plan["session_id"]}',migration_id=migration_id,state='pending')
        db.chat_migration_plans.replace_one({'_id':plan['_id']},plan,upsert=True)
        apply_chat(plan,user_id,kb_id,migration_id)
    db.chat_migrations.update_one({'_id':migration_id},{'$set':{'quarantined':quarantine,'updated_at':utcnow()}})
    return report


def apply_chat(plan,user_id,kb_id,migration_id):
    """
    按原记录快照迁移单个会话，检测到并发修改时停止
    :param plan: 包含原始消息、转换结果和目标会话 ID 的计划
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :param migration_id: 本批迁移的唯一标识，用于重试和回退
    """
    db=get_db();require_kb_permission(user_id,kb_id)
    # 所有消息完成前不创建 chat_sessions，未完成会话不能通过历史权限检查。
    for original,converted in zip(plan['original'],plan['converted']):
        after={**converted,'user_id':user_id,'kb_id':kb_id,'session_id':plan['session_id'],'migration_id':migration_id}
        # 重试时跳过已转换记录，只有与原快照完全一致的旧记录才允许替换。
        current=db.chat_message.find_one({'_id':original['_id']})
        if current==after:continue
        if current!=original:raise ValueError('旧消息已变化，停止迁移；请核对备份')
        replaced=db.chat_message.replace_one(original,after)
        if replaced.matched_count!=1:raise ValueError('迁移遇到并发修改；必须停服执行')
    db.chat_sessions.update_one({'_id':plan['session_id'],'user_id':user_id,'kb_id':kb_id},
        {'$setOnInsert':{'migration_id':migration_id,'created_at':utcnow()}},upsert=True)
    db.chat_migration_plans.update_one({'_id':plan['_id']},{'$set':{'state':'complete'}})


def rollback(user_id,kb_id,migration_id,apply=False):
    """
    校验迁移后记录未被改动，再预览或执行会话回退
    :param user_id: 服务端确定的用户 ID
    :param kb_id: 本次操作所属的知识库 ID
    :param migration_id: 本批迁移的唯一标识，用于重试和回退
    :param apply: 是否实际写入；False 时仅返回预览结果
    :return: 可恢复的消息数量
    """
    require_kb_permission(user_id,kb_id);db=get_db()
    journal=db.chat_migrations.find_one({'_id':migration_id,'user_id':user_id,'kb_id':kb_id})
    if not journal:raise ValueError('迁移记录不存在或归属不符')
    plans=list(db.chat_migration_plans.find({'migration_id':migration_id}));restore=[]
    for plan in plans:
        if db.chat_message.count_documents({'session_id':plan['session_id']})>len(plan['original']):raise ValueError('会话已有新增消息，拒绝回退')
        for original,converted in zip(plan['original'],plan['converted']):
            expected={**converted,'user_id':user_id,'kb_id':kb_id,'session_id':plan['session_id'],'migration_id':migration_id}
            current=db.chat_message.find_one({'_id':original['_id']})
            if current==original:continue
            if current!=expected:raise ValueError('迁移后消息已修改或删除，拒绝覆盖')
            restore.append((expected,original))
    # 全部消息校验通过后才开始回退，带条件替换避免覆盖迁移后的人工修改。
    if apply:
        for expected,original in restore:
            if db.chat_message.replace_one(expected,original).matched_count!=1:raise ValueError('回退遇到并发修改')
        db.chat_sessions.delete_many({'migration_id':migration_id,'user_id':user_id,'kb_id':kb_id})
        db.chat_migration_plans.update_many({'migration_id':migration_id},{'$set':{'state':'rolled_back'}})
    return {'restore_messages':len(restore)}


def main():
    """
    读取账号、知识库和迁移 ID 参数，执行预览、迁移或回退
    """
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id',required=True);parser.add_argument('--kb-id',required=True)
    parser.add_argument('--migration-id',required=True,type=lambda value:str(UUID(value)))
    parser.add_argument('--apply',action='store_true');parser.add_argument('--rollback',action='store_true')
    args=parser.parse_args()
    result=(rollback if args.rollback else migrate)(args.user_id,args.kb_id,args.migration_id,args.apply)
    print(json_util.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
