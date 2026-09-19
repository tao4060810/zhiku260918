"""账号回归：用户名唯一性、密码校验、登录有效期、CSRF 和限流。"""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pymongo.errors import ConnectionFailure, DuplicateKeyError
from support import DatabaseCase, PASSWORD, ORIGIN
from utils import auth_utils, user_store
from utils.rate_limit_utils import check_rate, RateLimited


class AuthTests(DatabaseCase):
    def register(self, **extra):
        return self.client.post('/auth/register', headers={'Origin': ORIGIN}, json={
            'username': 'Alice', 'password': PASSWORD, 'password_confirm': PASSWORD, **extra})

    def test_register_normalized_unique_private_and_no_privilege_input(self):
        self.assertEqual(self.register(role='admin').status_code, 422)
        self.assertEqual(self.register().status_code, 201)
        self.assertEqual(self.register(username='ALICE').status_code, 409)
        user = self.db.users.find_one()
        self.assertEqual(user['username'], 'alice')
        self.assertEqual(user['role'], 'user')
        self.assertTrue(user['password_hash'].startswith('$argon2id$'))
        self.assertNotIn(PASSWORD, str(user))
        self.assertEqual(self.db.knowledge_bases.count_documents({'owner_user_id': user['_id']}), 1)

    def test_concurrent_registration_unique_index(self):
        def create(_):
            try:
                return user_store.create_user('alice', 'hash')
            except DuplicateKeyError:
                return None
        with ThreadPoolExecutor(4) as pool:
            self.assertEqual(sum(x is not None for x in pool.map(create, range(4))), 1)
        self.assertEqual(self.db.knowledge_bases.count_documents({}), 1)

    def test_login_cookie_digest_and_same_wrong_password_error(self):
        user, _ = self.account()
        response = self.login()
        cookie = response.headers['set-cookie'].lower()
        self.assertIn('httponly', cookie);self.assertIn('samesite=lax', cookie)
        raw = self.client.cookies.get('zhiku_auth')
        self.assertNotIn(raw, str(self.db.auth_sessions.find_one()))
        errors=[]
        for name, password in [('alice','wrong'),('missing',PASSWORD)]:
            r=self.client.post('/auth/login',json={'username':name,'password':password})
            self.assertEqual(r.status_code,401);errors.append(r.json())
        self.db.users.update_one({'_id':user['_id']},{'$set':{'is_active':False}})
        r=self.client.post('/auth/login',json={'username':'alice','password':PASSWORD})
        self.assertEqual(r.json(), errors[0]);self.assertEqual(errors[0],errors[1])

    def test_expiry_idle_revocation_and_auth_version(self):
        user,_=self.account()
        for change in ('expiry','idle','revoke','version','disabled'):
            with self.subTest(change=change):
                self.db.users.update_one({'_id':user['_id']},{'$set':{'is_active':True,'auth_version':1}})
                self.login()
                raw=self.client.cookies.get('zhiku_auth')
                query={'token_hash':auth_utils.token_digest(raw)}
                if change=='expiry': self.db.auth_sessions.update_one(query,{'$set':{'expires_at':user_store.utcnow()-timedelta(seconds=1)}})
                elif change=='idle': self.db.auth_sessions.update_one(query,{'$set':{'last_seen_at':user_store.utcnow()-timedelta(hours=3)}})
                elif change=='revoke': auth_utils.revoke_session(raw)
                elif change=='version': self.db.users.update_one({'_id':user['_id']},{'$inc':{'auth_version':1}})
                else: self.db.users.update_one({'_id':user['_id']},{'$set':{'is_active':False}})
                self.assertEqual(self.client.get('/auth/me').status_code,401)

    def test_change_password_invalidates_all_sessions_and_logout_idempotent(self):
        user,_=self.account();self.login()
        other,_=auth_utils.issue_session(user)
        response=self.client.post('/auth/change-password',json={'current_password':PASSWORD,
            'new_password':'new-password-2026','password_confirm':'new-password-2026'})
        self.assertEqual(response.status_code,204)
        with self.assertRaises(auth_utils.LoginRequired):auth_utils.authenticate(other)
        self.assertEqual(self.client.post('/auth/logout').status_code,204)
        self.assertEqual(self.client.post('/auth/logout').status_code,204)

    def test_csrf_origin_and_upload_prevent_side_effects(self):
        _,kb=self.account();self.login()
        for headers in ({'X-CSRF-Token':''},{'Origin':'https://evil.test'},{'Origin':''}):
            response=self.client.post('/upload',params={'kb_id':kb['_id']},headers=headers,files={'files':('a.md',b'hello')})
            self.assertEqual(response.status_code,403)
        self.assertFalse(list(self.root.rglob('*.md')))
        self.assertEqual(self.db.documents.count_documents({}),0)
        self.assertEqual(self.client.post('/auth/register',json={
            'username':'bob','password':PASSWORD,'password_confirm':PASSWORD},headers={'Origin':'https://evil.test'}).status_code,403)

    def test_database_failure_is_503_and_limiter_bounded(self):
        self.account();self.login()
        with patch('utils.auth_utils.get_db',side_effect=ConnectionFailure()):
            self.assertEqual(self.client.get('/auth/me').status_code,503)
        with patch('utils.rate_limit_utils.MAX_BUCKETS',2):
            from utils import rate_limit_utils
            rate_limit_utils._buckets.clear()
            check_rate('a',1,100);check_rate('b',1,100)
            with self.assertRaises(RateLimited):check_rate('c',1,100)
            with self.assertRaises(RateLimited):check_rate('a',1,100)
            self.assertEqual(len(rate_limit_utils._buckets),2)

    def test_public_pages_do_not_load_business_scripts(self):
        for page in ('login','register'):
            response=self.client.get(f'/{page}.html')
            self.assertEqual(response.status_code,200)
            self.assertNotIn('/js/common.js',response.text)
            self.assertNotIn('/js/import-status.js',response.text)
        self.assertEqual(self.client.get('/sessions').status_code,401)
        self.assertEqual(self.client.get('/chat.html',follow_redirects=False).status_code,303)
