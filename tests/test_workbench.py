"""Public gateway integration: real stores and reporting, no network/model calls."""
from contextlib import closing
import asyncio
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

ORIGIN = 'http://127.0.0.1:18763'

class WorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.environment = patch.dict(os.environ, {
            'VOXQUARRY_DATA_DIR': cls.temp.name, 'VOXQUARRY_WORKERS': 'false',
            'VOXQUARRY_ORIGIN': ORIGIN, 'VOXQUARRY_INTERNAL_URL': ORIGIN,
            'VOXQUARRY_WENQUAN_HARNESS_ENABLED': 'false',
            'VOXQUARRY_LLM_API_KEY': '', 'VOXQUARRY_LLM_BASE_URL': '', 'VOXQUARRY_LLM_MODEL': '',
        })
        cls.environment.start()
        from voxquarry.server import create_app
        cls.app = create_app()
        cls.client = TestClient(cls.app, base_url=ORIGIN, headers={'Origin': ORIGIN})
        cls.client.__enter__()
        assert cls.client.get('/api/v1/auth/status').json()['setup_required']
        result = cls.client.post('/api/v1/auth/setup', json={'username': 'tester', 'password': 'synthetic-local-password-2026'})
        assert result.status_code == 200, result.text
        cls.user = result.json()['user']
        cls.client.headers['X-CSRF-Token'] = result.json()['csrf_token']
        cls.initial_cookie = cls.client.cookies.get('vq_session')
        result = cls.client.post('/api/v1/demo', json={})
        assert result.status_code == 200, result.text
        cls.demo = result.json()
        cls.report_path = '/reporting/api/v1/reports/' + cls.demo['report_id']

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.environment.stop()
        cls.temp.cleanup()

    def test_fresh_setup_cannot_be_repeated(self):
        r = self.client.post('/api/v1/auth/setup', json={'username': 'tester', 'password': 'synthetic-local-password-2026'})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.app.state.accounts.store.list_principals()), 1)

    def test_demo_is_idempotent(self):
        self.assertEqual(self.client.post('/api/v1/demo', json={}).json(), self.demo)

    def test_unauthenticated_and_forged_identity_are_denied(self):
        with closing(TestClient(self.app, base_url=ORIGIN)) as c:
            for path in ['/api/jobs', '/retail/api/v1/products', self.report_path, '/wenquan/api/v1/sessions']:
                self.assertEqual(c.get(path, headers={'X-VQ-Identity-Context': 'forged', 'X-VoxQuarry-Principal-Id': 'admin'}).status_code, 401)

    def test_origin_host_and_csrf(self):
        self.assertEqual(self.client.get('/health', headers={'Host': 'attacker.test'}).status_code, 400)
        self.assertEqual(self.client.post('/api/v1/demo', json={}, headers={'Origin': 'https://attacker.test'}).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/demo', json={}, headers={'X-CSRF-Token': 'wrong'}).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/auth/refresh', json={}, headers={'Origin': 'null'}).status_code, 403)

    def test_security_headers_and_cookie(self):
        r = self.client.get('/')
        self.assertIn("frame-ancestors 'none'", r.headers['content-security-policy'])
        self.assertEqual(r.headers['x-content-type-options'], 'nosniff')
        with closing(TestClient(self.app, base_url=ORIGIN, headers={'Origin': ORIGIN})) as c:
            r = c.post('/api/v1/auth/login', json={'username': 'tester', 'password': 'synthetic-local-password-2026'})
            self.assertIn('HttpOnly', r.headers['set-cookie'])
            self.assertIn('SameSite=strict', r.headers['set-cookie'])

    def test_payload_limits_and_invalid_length(self):
        for length, expected in [('invalid',400),('-1',400), (str(21*1024*1024),413)]:
            self.assertEqual(self.client.post('/api/v1/import', json={}, headers={'Content-Length':length}).status_code, expected)
        async def request():
            chunks=iter([{'type':'http.request','body':b'x'*(11*1024*1024),'more_body':True}, {'type':'http.request','body':b'x'*(11*1024*1024),'more_body':False}])
            messages=[]
            async def receive(): return next(chunks)
            async def send(value): messages.append(value)
            headers={'host':'127.0.0.1:18763','origin':ORIGIN,'cookie':'vq_session='+self.client.cookies.get('vq_session'),'x-csrf-token':self.client.headers['X-CSRF-Token']}
            scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST','scheme':'http','path':'/api/v1/import','query_string':b'','headers':[(k.encode(),v.encode()) for k,v in headers.items()],'client':('127.0.0.1',1),'server':('127.0.0.1',18763)}
            await self.app(scope,receive,send)
            return messages[0]['status']
        self.assertEqual(asyncio.run(request()),413)

    def test_service_routes_require_service_auth_even_for_admin(self):
        for path in ['/reporting/api/v1/agent-tools/zhuojian.get_report@v1','/reporting/api/v1/research-data/catalog','/api/research/catalog','/account-admin/internal/research-authorize']:
            self.assertEqual(self.client.post(path,json={}).status_code,401)

    def test_internal_report_catalog_uses_fresh_import(self):
        secret=json.loads((Path(self.temp.name)/'secrets.json').read_text())['gateway']
        headers={'X-VoxQuarry-Gateway-Token':secret,'X-VoxQuarry-Authority-Id':'local-test','X-VoxQuarry-Principal-Id':self.user['principal_id'],'X-VoxQuarry-Allowed-Report-Ids':'*'}
        r=self.client.post('/reporting/api/v1/agent-tools/zhuojian.list_reports@v1',json={},headers=headers)
        self.assertEqual(r.status_code,200,r.text)
        self.assertIn(self.demo['report_id'],r.text)

    def test_report_numbers_and_bilingual_export_are_same_facts(self):
        report=self.client.get(self.report_path).json()
        metrics={m['metric_id']:m for m in report['metrics']}
        self.assertEqual(metrics['positive_rating_rate']['numerator'],25)
        self.assertEqual(metrics['positive_rating_rate']['denominator'],40)
        self.assertEqual(metrics['positive_rating_rate']['value'],.625)
        for locale in ['en','zh-CN']:
            r=self.client.get(f"/api/v1/reports/{self.demo['report_id']}/export?locale={locale}")
            self.assertEqual(r.status_code,200,r.text)
            self.assertIn(report['hashes']['core_result'],r.text)
            self.assertIn('25',r.text)
            self.assertIn('40',r.text)
        self.assertEqual(self.client.get(self.report_path).json(),report)

    def test_evidence_keeps_original_text_and_identity(self):
        r=self.client.get(self.report_path+'/evidence?limit=20&include_review_text=true')
        self.assertEqual(r.status_code,200,r.text)
        evidence=r.json()['items']
        self.assertTrue(evidence)
        self.assertIn('Synthetic scenario',evidence[0]['review_text'])
        self.assertTrue(evidence[0]['evidence_id'])

    def test_evidence_paging_does_not_repeat_the_first_record(self):
        first=self.client.get(self.report_path+'/evidence?limit=1&offset=0&include_review_text=true').json()
        second=self.client.get(self.report_path+'/evidence?limit=1&offset=1&include_review_text=true').json()
        self.assertEqual(first['total'],second['total'])
        self.assertNotEqual(first['items'][0]['evidence_id'],second['items'][0]['evidence_id'])
        self.assertEqual(second['offset'],1)
        self.assertEqual(self.client.get(self.report_path+'/evidence?offset=-1').status_code,422)

    def test_topic_evidence_includes_neutral_mentions(self):
        report=self.client.get(self.report_path).json()
        topic=next(x for x in report['product_insight']['aspect_experience'] if x['aspect_id']=='battery_charger')
        self.assertEqual(topic['explicit_positive_entities'],0)
        self.assertEqual(topic['explicit_negative_entities'],0)
        self.assertEqual(topic['review_entities'],10)
        group=self.client.get(self.report_path+'/evidence-groups/'+topic['evidence_group_id']+'?limit=100&include_review_text=true')
        self.assertEqual(group.status_code,200,group.text)
        rows=group.json()['items']
        self.assertEqual(len({r['review_entity_id'] for r in rows}),10)
        self.assertTrue(all(r['label']=='battery_charger' for r in rows))
        self.assertTrue(all('Synthetic scenario' in r['review_text'] for r in rows))

    def test_chinese_retained_but_not_misrepresented_as_english_analysis(self):
        payload={'brand':'Synthetic','model':'CN','category':'Lawn Mower','language':'zh-CN','reviews':[{'review_id':'cn-1','rating':3,'review_text':'这是合成评论。电池续航不足，需要再次充电。'}]}
        r=self.client.post('/api/v1/import',json=payload)
        self.assertEqual(r.status_code,200,r.text)
        job=r.json()['job_id']
        r=self.client.post('/api/v1/reports',json={'report_name':'Chinese boundary test','scope':{'category_id':'walk_behind_mower','job_ids':[job]}})
        self.assertEqual(r.status_code,422,r.text)
        self.assertIn('SOURCE_LANGUAGE_UNVERIFIED',r.text)
        self.assertIn('这是合成评论', (Path(self.temp.name)/'source/data/runs'/job/'file_import/reviews.csv').read_text())

    def test_invalid_import_is_atomic(self):
        before=self.client.get('/api/jobs?limit=100').json()
        self.assertEqual(self.client.post('/api/v1/import',json={'reviews':[{'review_id':'a','rating':0}]}).status_code,422)
        self.assertEqual(before,self.client.get('/api/jobs?limit=100').json())

    def test_explicit_product_source_report_link(self):
        r=self.client.get('/api/v1/products')
        self.assertEqual(r.status_code,200,r.text)
        product=next(p for p in r.json()['items'] if p['id']==self.demo['product_id'])
        self.assertIn(self.demo['job_id'],product['source_job_ids'])
        self.assertIn(self.demo['report_id'],product['linked_report_ids'])
        self.assertEqual(self.client.post('/api/v1/products/'+product['id']+'/sources',json={'job_id':'missing'}).status_code,404)
        self.assertEqual(self.client.post('/api/v1/products/'+product['id']+'/sources',json={'job_id':self.demo['job_id']}).status_code,200)
        self.assertEqual(self.client.get('/api/v1/products').json()['items'][0]['source_job_ids'].count(self.demo['job_id']),1)

    def test_product_create_and_edit_preserve_id(self):
        r=self.client.post('/retail/api/v1/products',json={'category_code':'walk_behind_mower','brand':'Synthetic','model_number':'T1','product_name':'Test product'})
        self.assertEqual(r.status_code,201,r.text)
        item=r.json()
        pid=item.get('id') or item.get('item',{}).get('id')
        self.assertTrue(pid,r.text)
        r=self.client.put('/retail/api/v1/products/'+pid,json={'category_code':'walk_behind_mower','brand':'Synthetic','model_number':'T1','product_name':'Updated test'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertIn('Updated test',self.client.get('/retail/api/v1/products').text)

    def test_local_authority_tracks_account_state(self):
        from account_admin.research_authority import ResearchAuthority
        authority=ResearchAuthority(self.app.state.accounts)
        status,value=authority.authorize(self.user['principal_id'],'chat.session.own')
        self.assertEqual(status,200)
        self.assertTrue(value['authorization_version'].startswith('authv_'))
        self.assertNotIn('password',json.dumps(value))
        self.assertEqual(authority.authorize('missing','chat.session.own')[0],403)
        self.assertEqual(authority.authorize(self.user['principal_id'],'admin.manage_users')[0],403)

    def test_model_absence_is_visible(self):
        r=self.client.get('/api/v1/settings').json()
        self.assertFalse(r['llm_configured'])
        self.assertEqual(r['analysis_languages'],['en'])
        self.assertIn('walmart',r['channels']);self.assertIn('homedepot',r['channels'])
        self.assertNotIn('api_key',json.dumps(r))

    def test_owner_isolation_and_master_data_permissions(self):
        service=self.app.state.accounts
        credentials=service.register_local_account(employee_id='member',display_name='Member',password='synthetic-member-password',remember_me=False,request_id='synthetic-register',client_ip='127.0.0.1',user_agent='test')
        s=self.client.post('/wenquan/api/v1/sessions',json={'title':'Private session'})
        self.assertEqual(s.status_code,201,s.text)
        sid=s.json()['session']['session_id']
        with closing(TestClient(self.app,base_url=ORIGIN,headers={'Origin':ORIGIN,'X-CSRF-Token':credentials.csrf_token})) as c:
            c.cookies.set('vq_session',credentials.token)
            self.assertEqual(c.get('/wenquan/api/v1/sessions/'+sid).status_code,404)
            self.assertEqual(c.post('/api/admin/master-data/brands',json={}).status_code,403)
            self.assertEqual(c.post('/api/v1/demo',json={}).status_code,403)
            own=c.post('/wenquan/api/v1/sessions',json={'title':'Own session'})
            self.assertEqual(own.status_code,201,own.text)

    def test_refresh_rotates_cookie_and_returns_working_csrf(self):
        with closing(TestClient(self.app,base_url=ORIGIN,headers={'Origin':ORIGIN})) as c:
            r=c.post('/api/v1/auth/login',json={'username':'tester','password':'synthetic-local-password-2026'})
            old=c.cookies.get('vq_session')
            r=c.post('/api/v1/auth/refresh',json={})
            self.assertEqual(r.status_code,200,r.text)
            self.assertNotEqual(c.cookies.get('vq_session'),old)
            self.assertEqual(c.post('/api/v1/auth/logout',json={},headers={'X-CSRF-Token':r.json()['csrf_token']}).status_code,200)
            self.assertEqual(c.get('/api/v1/me').status_code,401)

if __name__=='__main__':unittest.main()
