"""本机管理命令。密码通过隐藏输入读取，不接受命令行明文密码。"""
import argparse
from getpass import getpass
from dotenv import load_dotenv
load_dotenv()

from utils.auth_utils import passwords
from utils.user_store import create_user, get_db, utcnow
from utils.knowledge_store import ensure_default_kb
from web.api.auth_service import RegisterBody


def main():
    """
    解析本机账号管理命令，执行创建、查看、停用或密码重置
    """
    # 1. 解析管理动作；仅本机命令可指定创建账号时的角色
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['create','list','disable','enable','reset-password'])
    parser.add_argument('--username')
    parser.add_argument('--role',choices=['user','admin'],default='user')
    args=parser.parse_args()
    db=get_db()
    if args.action=='list':
        for user in db.users.find({}, {'username':1,'role':1,'is_active':1}):
            print(user['_id'],user['username'],user['role'],'active' if user['is_active'] else 'disabled')
        return
    if not args.username:parser.error('需要 --username')
    username=args.username.strip().lower()
    # 2. 创建或重置密码时隐藏输入，复用接口的密码长度和确认校验
    if args.action in {'create','reset-password'}:
        password=getpass('密码（12–128 个字符）: ')
        body=RegisterBody(username=username,password=password,password_confirm=getpass('确认密码: '))
        hashed=passwords.hash(body.password)
    if args.action=='create':
        user=create_user(username,hashed,args.role)
        print('user_id:',user['_id'],'kb_id:',ensure_default_kb(user['_id'])['_id'])
        return
    values={'updated_at':utcnow()}
    if args.action=='reset-password':values['password_hash']=hashed
    else:values['is_active']=args.action=='enable'
    # 3. 停用、启用或重置后递增认证版本，使已有登录凭证统一失效
    result=db.users.update_one({'username':username},{'$set':values,'$inc':{'auth_version':1}})
    if not result.matched_count:parser.error('账号不存在')
    print('已更新，已有登录凭证失效。')


if __name__=='__main__':main()
