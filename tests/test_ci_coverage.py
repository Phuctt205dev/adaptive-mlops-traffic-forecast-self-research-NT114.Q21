"""Coverage selection and deployment acceptance must fail closed."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.request import Request

from scripts.cicd import ci_scope as scope,ci_coverage as coverage,github_gate
from tests import test_cicd as gate_tests

SHA='a'*40;BASE='b'*40


def identity():
    return {'release_id':'c'*64,'images':{role:{'role':role,'commit':BASE,'source_id':'d'*64,
            'image':'ghcr.io/owner/repo/backend@sha256:'+'e'*64} for role in ('api','tree','neural','sync')}}


def suite(mode='current'):
    cases=[]
    for name in coverage.CASES[mode]:
        cases.append({'case':name,'queued_database_rows':1,'guard_held':False,'recovery_required':False,
                      'up_to_date':True,'deferred':True,'rolled_back':True,'completed':True,
                      'flush_proofs':[{'complete':True,'stable':True}]})
    return {'schema':'traffic-isolated-delivery-proof/v2','suite':mode,'source_commit':SHA,
            'ci_run_id':'1','ci_attempt':'2','kubernetes_context':'kind-cd-e2e',**identity(),
            'production_accessed':False,'business_http_is_fixture':True,'real_baseline_and_role_images':True,
            'real_rf_inference':True,'real_database_and_object_storage':True,'provider_called':False,
            'long_request_completed':True,'queue_preserved':True,'cases':cases,'duration_seconds':100}


def acceptance(full=False):
    selected=scope.classify(['deployment/rke2/runtime-contract.json'] if full else ['backend/app/main.py'],baseline=BASE,commit=SHA)
    proofs={mode:suite(mode) for mode in (('current','migration') if full else ('current',))}
    results={k:{'result':'success'} for k in coverage.required_results(selected)}
    return coverage.aggregate(selected,proofs,results,commit=SHA,run_id='1',attempt='2',identity=identity())


class ScopeTests(unittest.TestCase):
    def test_ordinary_application_changes_keep_all_regular_safety_tests(self):
        for paths in (['backend/app/main.py'],['src/features.py','tests/test_features.py'],['scripts/cloud_training/execute_branch.py'],[]):
            with self.subTest(paths=paths):
                result=scope.classify(paths,baseline=BASE,commit=SHA)
                self.assertFalse(result['full']);scope.validate(result,SHA)

    def test_foundation_unknown_and_no_baseline_always_require_full(self):
        for path in ('scripts/cicd/roles_deploy.py','deployment/rke2/roles/api.json','requirements/roles/api.txt',
                     'tests/integration/runtime_fixture.py','.github/workflows/backend-ci.yml','other/new_file.py'):
            with self.subTest(path=path):self.assertTrue(scope.classify([path],baseline=BASE,commit=SHA)['full'])
        self.assertTrue(scope.classify([],baseline=None,commit=SHA)['full'])
        self.assertTrue(scope.classify([],baseline=BASE,commit=SHA,force=True)['full'])

    def test_missing_diverged_or_shallow_history_increases_coverage(self):
        for error in (ValueError(),subprocess.CalledProcessError(1,['git']),OSError()):
            with self.subTest(error=type(error).__name__):
                value=scope.select('.',SHA,state={'last_success':{'commit':BASE}},diff=Mock(side_effect=error))
                self.assertTrue(value['full']);self.assertIsNone(value['baseline']);scope.validate(value,SHA)

    def test_multiple_commits_rename_and_delete_include_old_foundation_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def git(*args):
                return subprocess.run(['git','-C',directory,*args],check=True,capture_output=True,text=True).stdout.strip()
            git('init');git('config','user.name','CI fixture');git('config','user.email','ci@example.test')
            (root/'deployment').mkdir();(root/'backend').mkdir()
            (root/'deployment/old.py').write_text('important\n');(root/'backend/deleted.py').write_text('remove\n')
            git('add','.');git('commit','-m','baseline');baseline=git('rev-parse','HEAD')
            (root/'deployment/old.py').rename(root/'backend/moved.py');(root/'backend/deleted.py').unlink()
            git('add','-A');git('commit','-m','rename foundation into ordinary directory')
            (root/'backend/latest.py').write_text('latest\n');git('add','.');git('commit','-m','ordinary final commit')
            commit=git('rev-parse','HEAD');paths=scope.git_paths(root,baseline,commit)
            self.assertEqual(set(paths),{'deployment/old.py','backend/moved.py','backend/deleted.py','backend/latest.py'})
            result=scope.select(root,commit,state={'last_success':{'commit':baseline}})
            self.assertTrue(result['full']);self.assertEqual(result['baseline'],baseline)

    def test_scope_cannot_downgrade_or_change_source(self):
        value=scope.classify(['deployment/roles.json'],baseline=BASE,commit=SHA)
        for changes in ({'full':False},{'commit':BASE},{'reason':'ordinary_source_change'},{'paths':['../backend/main.py']}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):scope.validate({**value,**changes},SHA)


class AcceptanceTests(unittest.TestCase):
    def check(self,proof,*,jobs=None,run_changes=None):
        names=coverage.COMMON_JOBS|({'legacy-migration'} if proof['scope']['full'] else set())
        if jobs is None:jobs=[{'name':n,'status':'completed','conclusion':'success'} for n in names]
        run={'id':1,'head_sha':SHA,'run_attempt':2,**(run_changes or {})}
        return coverage.check_run('owner/repo',run,Mock(return_value={'jobs':jobs}),artifact=Mock(return_value=proof))

    def test_regular_coverage_accepts_intentional_migration_skip_only(self):
        proof=acceptance();jobs=[{'name':n,'status':'completed','conclusion':'success'} for n in coverage.COMMON_JOBS]
        jobs.append({'name':'legacy-migration','status':'completed','conclusion':'skipped'})
        self.assertTrue(self.check(proof,jobs=jobs)['deployable'])

    def test_full_coverage_requires_both_real_suites(self):
        self.assertTrue(self.check(acceptance(True))['deployable'])
        proof=acceptance(True);del proof['suites']['migration']
        with self.assertRaisesRegex(ValueError,'coverage differs'):self.check(proof)

    def test_required_job_skipped_failed_running_missing_or_duplicate_blocks(self):
        proof=acceptance(True);names=coverage.COMMON_JOBS|{'legacy-migration'}
        for name in names:
            for result in ('skipped','failure','cancelled','running','missing','duplicate'):
                jobs=[{'name':n,'status':'completed','conclusion':'success'} for n in names if n!=name]
                if result!='missing':jobs.append({'name':name,'status':'in_progress' if result=='running' else 'completed','conclusion':result})
                if result=='duplicate':jobs.extend([{'name':name,'status':'completed','conclusion':'success'}]*2)
                with self.subTest(name=name,result=result),self.assertRaises(ValueError):self.check(proof,jobs=jobs)

    def test_wrong_commit_attempt_or_run_cannot_reuse_old_acceptance(self):
        for changes in ({'head_sha':BASE},{'id':2},{'run_attempt':3}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.check(acceptance(),run_changes=changes)

    def test_progress_incomplete_or_fabricated_identity_is_rejected(self):
        for changes in ({'schema':'traffic-isolated-delivery-progress/v1'},{'source_commit':BASE},
                        {'ci_attempt':'1'},{'production_accessed':True},{'provider_called':True},
                        {'queue_preserved':False},{'long_request_completed':False},{'cases':[]}):
            proof=acceptance();proof['suites']['current'].update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.check(proof)

    def test_missing_case_failed_recovery_or_unacknowledged_flush_blocks(self):
        for field,value in (('queued_database_rows',0),('guard_held',True),('recovery_required',True),('flush_proofs',[]),('completed',False)):
            proof=acceptance();case=next(c for c in proof['suites']['current']['cases'] if c['case']=='manifest_only_update')
            case[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.check(proof)

    def test_suites_must_check_the_packaged_images_and_release(self):
        for field,value in (('release_id','f'*64),('images',{**identity()['images'],'sync':{**identity()['images']['sync'],'source_id':'f'*64}})):
            proof=acceptance(True);proof['suites']['migration'][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'another immutable release'):self.check(proof)

    def test_aggregate_cannot_hide_failure_with_successful_optional_jobs(self):
        proof=acceptance(True)
        for name in coverage.required_results(proof['scope']):
            results={k:{'result':'success'} for k in coverage.required_results(proof['scope'])};results[name]['result']='skipped'
            with self.subTest(name=name),self.assertRaises(ValueError):
                coverage.aggregate(proof['scope'],proof['suites'],results,commit=SHA,run_id=1,attempt=2,identity=identity())

    def test_deploy_gate_actually_requires_coverage_after_policy_validation(self):
        values=gate_tests.GateTests().responses();values[0].update(id=1,run_attempt=2)
        required=Mock(side_effect=ValueError('missing coverage'))
        with self.assertRaisesRegex(ValueError,'missing coverage'):
            github_gate.validate('owner/repo','1',fetch=Mock(side_effect=values),coverage=required)
        required.assert_called_once()

    def test_artifact_lookup_rejects_missing_expired_or_ambiguous_proof(self):
        for artifacts in ([],[{'name':'proof','expired':True}], [{'name':'proof','expired':False}]*2):
            with self.subTest(artifacts=artifacts),self.assertRaises(ValueError):
                coverage.read_artifact('owner/repo','1','proof',Mock(return_value={'artifacts':artifacts}))

    def test_redirect_never_sends_github_token_to_artifact_storage(self):
        handler=coverage.SafeRedirect();request=Request('https://api.github.com/archive',headers={'Authorization':'Bearer test-only'})
        redirect=handler.redirect_request(request,None,302,'redirect',{},'https://storage.example.test/file.zip')
        self.assertIsNone(redirect.get_header('Authorization'))
        with self.assertRaises(ValueError):handler.redirect_request(request,None,302,'redirect',{},'http://storage.example.test/file.zip')


if __name__=='__main__':unittest.main()
