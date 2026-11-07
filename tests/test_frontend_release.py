"""A combined or concurrent push cannot publish against an unverified backend."""
import base64
import json
import unittest
from unittest.mock import patch

from scripts.cicd import cd_state as cd,frontend_release as frontend,github_gate
from tests.test_cd_control import MemoryAPI,transaction
from tests import test_cd_control as policy_fixtures

A='a'*40;B='b'*40
CONFIG={'FRONTEND_API_URL':'https://api.example.com/api/v1','FRONTEND_BASE_PATH':'/repo/'}


class API(MemoryAPI):
    def __init__(self):
        state=cd.initial();state['last_success']=transaction();state['last_success']['commit']=A
        super().__init__(state);self.publication=None;self.calls=[]
    def request(self,method,path,body=None):
        self.calls.append((method,path,body))
        if method=='GET' and path=='':return {'default_branch':'master'}
        if method=='GET' and path.startswith('git/ref/heads/master'):return {'object':{'sha':B}}
        if path.startswith('contents/frontend-publication.json'):
            if self.publication is None:raise cd.ApiError(404)
            return {'sha':'1'*40,'content':base64.b64encode(json.dumps(self.publication).encode()).decode()}
        if method=='POST' and 'dispatches' in path:return {}
        return super().request(method,path,body)


class PublicationPolicy(unittest.TestCase):
    def snapshot(self,backend='same',frontend_tree='frontend'):
        return {'frontend':frontend_tree,'backend':{'backend':backend},'workflows':{}}

    @patch.dict('os.environ',CONFIG)
    def test_frontend_only_ahead_of_verified_backend_is_allowed(self):
        api=API();trees={A:self.snapshot(),B:self.snapshot()}
        result=frontend.select(api,B,trees=lambda c,s:trees[s])
        self.assertTrue(result['publish']);self.assertEqual(result['backend_commit'],A)

    @patch.dict('os.environ',CONFIG)
    def test_combined_push_waits_for_matching_backend_content(self):
        api=API();trees={A:self.snapshot(),B:self.snapshot(backend='unverified')}
        self.assertFalse(frontend.select(api,B,trees=lambda c,s:trees[s])['publish'])
        api.state['last_success']['commit']=B
        self.assertTrue(frontend.select(api,B,trees=lambda c,s:trees[s])['publish'])

    @patch.dict('os.environ',CONFIG)
    def test_old_frontend_cannot_overwrite_newer_frontend(self):
        api=API();trees={A:self.snapshot(frontend_tree='old'),B:self.snapshot(frontend_tree='new')}
        self.assertFalse(frontend.select(api,A,trees=lambda c,s:trees[s])['publish'])

    @patch.dict('os.environ',CONFIG)
    def test_published_content_is_noop_but_a_config_change_is_not(self):
        api=API();trees=lambda c,s:self.snapshot()
        first=frontend.select(api,B,trees=trees)
        api.publication={'schema':'traffic-frontend-publication/v1','commit':B,'deployment_id':first['deployment_id']}
        self.assertFalse(frontend.select(api,B,trees=trees)['publish'])
        with patch.dict('os.environ',{'FRONTEND_BASE_PATH':'/another/'}):
            self.assertTrue(frontend.select(api,B,trees=trees)['publish'])

    @patch.dict('os.environ',CONFIG)
    def test_approval_identity_cannot_be_reused_from_another_backend_run(self):
        self.assertFalse(frontend.select(API(),B,'another-run',trees=lambda c,s:self.snapshot())['publish'])

    def test_backend_completion_resumes_the_latest_compatible_frontend(self):
        api=API();run_id=api.state['last_success']['run_id']
        with patch.object(frontend,'select',return_value={'publish':True,'sha':B}):
            self.assertTrue(frontend.dispatch(api,A,run_id)['frontend_dispatched'])
        post=next(body for method,path,body in api.calls if method=='POST')
        self.assertEqual(post['inputs']['source_sha'],B)

    def test_migration_cannot_select_the_auto_environment(self):
        state=cd.initial();state['mode']='auto'
        with self.assertRaisesRegex(ValueError,'Manual gate'):
            github_gate.validate('owner/repo','1',fetch=policy_fixtures.PolicyTests().fetch(state),operation='migrate-role-runtime-v1')


if __name__=='__main__':unittest.main()
