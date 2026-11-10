"""Application delivery contracts: optional checks, provenance and stale pushes."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.cicd import pipeline, ci_coverage, github_gate, frontend_release, prepare_artifact, cd_pending
from scripts.cicd.release import canonical, digest
from tests.test_ci_coverage import identity as old_identity, suite
from tests import test_cicd as gate_fixtures
from tests.test_prepare_artifact import FakeClient

A='a'*40
B='b'*40
ENV={'GITHUB_EVENT_NAME':'push','GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'2','GITHUB_SHA':A}
CONFIG={'api_url':'https://api.example.com/api/v1','base_path':'/repo/'}


def delivery_identity():
    return {**old_identity(), 'schema':'traffic-delivery/v2','commit':A,
            'image':old_identity()['images']['api']['image']}


def proof_for(paths, *, frontend=False):
    scope=pipeline.classify(paths,baseline=B,commit=A,frontend=frontend)
    proofs={mode:suite(mode) for mode,selected in [('current',scope['current']),('migration',scope['full'])] if selected}
    results={name:{'result':'success'} for name in pipeline.required_results(scope)}
    return pipeline.aggregate(scope,proofs,results,commit=A,run_id=1,attempt=2,
                              identity=delivery_identity() if scope['backend'] else None)


class ApplicationScopeTests(unittest.TestCase):
    def test_ordinary_push_still_skips_an_already_published_frontend(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            env={**ENV,'GITHUB_REPOSITORY':'owner/repo','GITHUB_OUTPUT':str(root/'outputs')}
            identifier=digest(canonical({'source':'frontend-tree',**CONFIG}))
            with patch.dict(os.environ,env,clear=True),patch.object(pipeline,'GitHub') as client, \
                 patch.object(pipeline,'Store') as store,patch.object(pipeline.ci_scope,'git_paths',return_value=[]), \
                 patch.object(frontend_release,'PublicationStore') as published, \
                 patch.object(frontend_release,'frontend_config',return_value=CONFIG), \
                 patch.object(pipeline.subprocess,'run',return_value=Mock(stdout='frontend-tree\n')), \
                 patch.object(pipeline,'Path',side_effect=lambda p:root/p) as paths,redirect_stdout(io.StringIO()):
                paths.cwd.return_value=root
                client.return_value.repository='owner/repo'
                store.return_value.load.return_value=({'last_success':{'commit':B}},None)
                published.return_value.load.return_value=({'deployment_id':identifier},None)
                pipeline.scope_main()
            scope=json.loads((root/'ci-scope.json').read_text())
            for flag in ('backend','tests','frontend','current','full','forced'):self.assertFalse(scope[flag])

    def test_manual_and_weekly_full_check_include_frontend_even_if_already_published(self):
        for event in ('workflow_dispatch','schedule'):
            with self.subTest(event=event),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                env={**ENV,'GITHUB_EVENT_NAME':event,'GITHUB_REPOSITORY':'owner/repo',
                     'GITHUB_OUTPUT':str(root/'outputs')}
                with patch.dict(os.environ,env,clear=True),patch.object(pipeline,'GitHub'), \
                     patch.object(pipeline,'Store') as store,patch.object(pipeline.ci_scope,'git_paths',return_value=[]), \
                     patch.object(frontend_release,'PublicationStore') as published, \
                     patch.object(pipeline,'Path',side_effect=lambda p:root/p) as paths,redirect_stdout(io.StringIO()):
                    paths.cwd.return_value=root
                    store.return_value.load.return_value=({'last_success':{'commit':B}},None)
                    pipeline.scope_main()
                    published.assert_not_called()
                scope=json.loads((root/'ci-scope.json').read_text())
                for flag in ('backend','tests','frontend','current','full','forced'):
                    self.assertTrue(scope[flag])
                    if flag!='forced':self.assertIn(flag+'=true\n',(root/'outputs').read_text())

    def test_frontend_tests_docs_and_app_have_distinct_work(self):
        cases=[(['frontend/src/main.jsx'],False,False,False),
               (['tests/test_prediction_service.py'],False,True,False),
               (['docs/guides/MODEL_QUALITY.md'],False,False,False),
               (['backend/app/services/prediction.py'],True,True,False),
               (['docs/index.html'],True,True,False),
               (['backend/app/services/restore_cache.py'],True,True,True)]
        for paths,backend,tests,current in cases:
            with self.subTest(paths=paths):
                scope=pipeline.classify(paths,baseline=B,commit=A)
                self.assertEqual((scope['backend'],scope['tests'],scope['current']),(backend,tests,current))
                pipeline.validate_scope(scope,A)

    def test_unknown_history_legacy_and_acceptance_contract_require_full(self):
        for paths,baseline,force in [([],None,False),(['other/input'],B,False),([],B,True),
                    (['deployment/rke2/migrations/role-runtime-v1.json'],B,False),
                    (['scripts/cicd/pipeline.py'],B,False)]:
            with self.subTest(paths=paths,baseline=baseline):
                scope=pipeline.classify(paths,baseline=baseline,commit=A,force=force)
                self.assertTrue(scope['full']);self.assertTrue(scope['current']);self.assertTrue(scope['backend'])

    def test_accumulated_runtime_change_cannot_be_hidden_by_a_later_app_edit(self):
        scope=pipeline.classify(['backend/app/services/prediction.py','deployment/rke2/roles/api.json'],baseline=B,commit=A)
        self.assertTrue(scope['current'])
        for field in ('backend','tests','current'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                pipeline.validate_scope({**scope,field:False},A)


class PullRequestChecksTests(unittest.TestCase):
    def test_pr_work_is_selected_from_its_own_changes(self):
        cases = [(['README.md', 'docs/guides/CICD_RKE2.md'], False, False),
                 (['frontend/src/main.jsx'], False, True),
                 (['backend/app/services/prediction.py'], True, False),
                 (['tests/test_prediction_service.py'], True, False),
                 (['docs/index.html'], True, False),
                 (['.github/workflows/backend-ci.yml'], True, True),
                 (['unfamiliar/input'], True, True)]
        for paths, tests, frontend in cases:
            with self.subTest(paths=paths):
                scope = pipeline.pr_scope(paths, commit=A, base=B)
                self.assertEqual((scope['tests'], scope['frontend']), (tests, frontend))
                self.assertEqual(pipeline.validate_pr_scope(scope, A, B), scope)
                self.assertFalse(scope['current']); self.assertFalse(scope['full'])

    def test_pr_scope_needs_no_production_state_or_fork_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**ENV, 'GITHUB_EVENT_NAME': 'pull_request', 'PR_BASE_SHA': B,
                   'GITHUB_OUTPUT': str(root/'outputs')}
            with patch.dict(os.environ, env, clear=True), patch.object(pipeline, 'GitHub') as github, \
                 patch.object(pipeline.ci_scope, 'git_paths', return_value=['README.md']), \
                 patch.object(pipeline, 'Path', side_effect=lambda p: root/p) as paths, redirect_stdout(io.StringIO()):
                paths.cwd.return_value = root
                pipeline.scope_main()
                github.assert_not_called()
            scope = json.loads((root/'ci-scope.json').read_text())
            self.assertFalse(scope['tests']); self.assertFalse(scope['frontend'])

    def test_unavailable_diff_requires_both_checks(self):
        for base, paths in [(None, []), (B, None)]:
            with self.subTest(base=base, paths=paths):
                scope = pipeline.pr_scope(paths, commit=A, base=base)
                self.assertTrue(scope['tests']); self.assertTrue(scope['frontend'])
                pipeline.validate_pr_scope(scope, A, B)
                with self.assertRaises(ValueError):
                    pipeline.validate_pr_scope({**scope, 'tests': False}, A, B)

    def test_base_identity_and_required_checks_cannot_be_downgraded(self):
        scope = pipeline.pr_scope(['backend/app/main.py'], commit=A, base=B)
        for damaged in ({**scope, 'base': 'c'*40}, {**scope, 'tests': False}, {**scope, 'commit': B}):
            with self.assertRaises(ValueError):
                pipeline.validate_pr_scope(damaged, A, B)
        for result in ('skipped', 'failure', 'cancelled', None):
            with self.subTest(result=result), self.assertRaises(ValueError):
                pipeline.require_pr_checks(scope, {'scope': {'result': 'success'},
                    'tests': {'result': result}, 'frontend-build': {'result': 'skipped'}})

    def test_pr_flags_reject_integer_and_string_coercion(self):
        scope = pipeline.pr_scope(['README.md'], commit=A, base=B)
        for value in (0, 1, 'false', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pipeline.validate_pr_scope({**scope, 'tests': value}, A, B)

    def test_docs_gate_accepts_skipped_heavy_jobs_but_not_failed_scope(self):
        scope = pipeline.pr_scope(['README.md'], commit=A, base=B)
        results = {'scope': {'result': 'success'}, 'tests': {'result': 'skipped'},
                   'frontend-build': {'result': 'skipped'}}
        pipeline.require_pr_checks(scope, results)
        with self.assertRaises(ValueError):
            pipeline.require_pr_checks(scope, {**results, 'scope': {'result': 'failure'}})
        with self.assertRaises(ValueError):
            pipeline.require_pr_checks(scope, {**results, 'frontend-build': {'result': 'failure'}})
        with self.assertRaises(ValueError):
            pipeline.validate_scope(scope, A)

    def test_docs_acceptance_never_produces_a_deployable_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'coverage').mkdir()
            scope = pipeline.pr_scope(['README.md'], commit=A, base=B)
            (root/'coverage/ci-scope.json').write_text(json.dumps(scope))
            results = {name: {'result': result} for name, result in
                       [('scope', 'success'), ('tests', 'skipped'), ('frontend-build', 'skipped')]}
            env = {**ENV, 'GITHUB_EVENT_NAME': 'pull_request', 'PR_BASE_SHA': B,
                   'CI_JOB_RESULTS': json.dumps(results), 'GITHUB_OUTPUT': str(root/'outputs')}
            with patch.dict(os.environ, env, clear=True), patch.object(pipeline, 'Path', side_effect=lambda p: root/p), redirect_stdout(io.StringIO()):
                pipeline.accept_main()
            self.assertFalse((root/'coverage/ci-acceptance.json').exists())
            self.assertIn('deployable=false', (root/'outputs').read_text())


class ApplicationAcceptanceTests(unittest.TestCase):
    def test_app_fast_path_needs_no_controller_proof_but_every_image_job(self):
        proof=proof_for(['backend/app/services/prediction.py'])
        self.assertEqual(proof['suites'],{})
        scope=pipeline.check_acceptance(proof,{'id':1,'head_sha':A,'run_attempt':2})
        self.assertNotIn('current-runtime',pipeline.required_jobs(scope))
        self.assertIn('image (neural)',pipeline.required_jobs(scope))

    def test_frontend_only_has_no_image_or_backend_deploy_permission(self):
        proof=proof_for(['frontend/src/main.jsx'],frontend=True)
        self.assertIsNone(proof['identity'])
        self.assertEqual(pipeline.required_jobs(proof['scope']),{'scope','frontend-build','acceptance'})
        with self.assertRaisesRegex(ValueError,'no backend release'):
            ci_coverage.check_run('owner/repo',{'id':1,'head_sha':A,'run_attempt':2},Mock(),artifact=Mock(return_value=proof))

    def test_required_skipped_or_failed_job_cannot_be_hidden(self):
        proof=proof_for(['backend/app/services/restore_cache.py'],frontend=True)
        scope=proof['scope']
        for name in pipeline.required_results(scope):
            for outcome in ('skipped','failure','cancelled'):
                results={n:{'result':'success'} for n in pipeline.required_results(scope)}
                results[name]['result']=outcome
                with self.subTest(job=name,outcome=outcome),self.assertRaises(ValueError):
                    pipeline.aggregate(scope,proof['suites'],results,commit=A,run_id=1,attempt=2,identity=proof['identity'])

    def test_runtime_changes_require_exact_suite_images_and_attempt(self):
        proof=proof_for(['backend/app/services/restore_cache.py'])
        for mutate in ('missing','attempt','image','identity'):
            damaged=copy.deepcopy(proof)
            if mutate=='missing':damaged['suites']={}
            if mutate=='attempt':damaged['suites']['current']['ci_attempt']='1'
            if mutate=='image':damaged['suites']['current']['images']['sync']['source_id']='f'*64
            if mutate=='identity':del damaged['identity']['images']['neural']
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):
                pipeline.check_acceptance(damaged,{'id':1,'head_sha':A,'run_attempt':2})

    def test_actual_jobs_must_match_this_attempt_not_just_a_green_proof(self):
        proof=proof_for(['frontend/src/main.jsx'],frontend=True)
        jobs=[{'name':name,'status':'completed','conclusion':'success'} for name in pipeline.required_jobs(proof['scope'])]
        run={'id':1,'head_sha':A,'run_attempt':2}
        self.assertTrue(ci_coverage.check_run('owner/repo',run,Mock(return_value={'jobs':jobs}),
                        artifact=Mock(return_value=proof),require_backend=False)['deployable'])
        jobs[0]['conclusion']='skipped'
        with self.assertRaises(ValueError):
            ci_coverage.check_run('owner/repo',run,Mock(return_value={'jobs':jobs}),artifact=Mock(return_value=proof),require_backend=False)


class SameRunAndFreshnessTests(unittest.TestCase):
    def current(self):
        values=gate_fixtures.GateTests().responses()
        values[0].update(id=1,run_attempt=2,status='in_progress',conclusion=None)
        return values

    @patch.dict(os.environ,ENV,clear=True)
    def test_own_in_progress_run_requires_completed_ci_acceptance(self):
        values=self.current()
        with patch.object(pipeline,'current_source',return_value=True):
            selected=github_gate.validate('owner/repo',1,fetch=Mock(side_effect=values),expected_commit=A,
                          expected_attempt=2,current_run=True,coverage=Mock(return_value=proof_for(['backend/app/prediction.py'])))
        self.assertEqual(selected['eligible'],'true')
        self.assertEqual(selected['application'],'true')

    @patch.dict(os.environ,ENV,clear=True)
    def test_other_run_rerun_and_pr_cannot_use_same_run_exception(self):
        base=self.current()[0]
        self.assertTrue(github_gate.same_run(base,1,A,2))
        for run_id,commit,attempt in [(2,A,2),(1,B,2),(1,A,1)]:
            self.assertFalse(github_gate.same_run(base,run_id,commit,attempt))
        with patch.dict(os.environ,{'GITHUB_EVENT_NAME':'pull_request'}):
            self.assertFalse(github_gate.same_run(base,1,A,2))

    @patch.dict(os.environ,ENV,clear=True)
    def test_artifact_preparation_allows_only_own_current_run(self):
        client=FakeClient(b'fake archive')
        client.run.update(id=1,head_sha=A,run_attempt=2,status='in_progress',conclusion=None)
        client.item['workflow_run'].update(id=1,head_sha=A)
        self.assertEqual(prepare_artifact.select_artifact(client,1,A,current_run=True)['id'],123)
        with self.assertRaises(prepare_artifact.PreparationError):prepare_artifact.select_artifact(client,1,A)
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'2'}),self.assertRaises(prepare_artifact.PreparationError):
            prepare_artifact.select_artifact(client,1,A,current_run=True)

    def test_source_comparison_ignores_docs_but_not_runtime_or_deleted_files(self):
        for changed,expected in [('docs/guides/README.md',True),('backend/app/main.py',False),
                                  ('.github/workflows/backend-ci.yml',False)]:
            old=[{'path':'backend/app/main.py','type':'blob','sha':'same'}]
            newer=old+[{'path':changed,'type':'blob','sha':'new'}] if changed!='backend/app/main.py' else []
            values=[{'default_branch':'master'},{'object':{'sha':B}}, {'tree':{'sha':'oldtree'}},
                    {'truncated':False,'tree':old},{'tree':{'sha':'newtree'}},{'truncated':False,'tree':newer}]
            with self.subTest(changed=changed):
                self.assertEqual(pipeline.current_source('owner/repo',A,Mock(side_effect=values)),expected)

    def test_incomplete_tree_never_proves_source_unchanged(self):
        values=[{'default_branch':'master'},{'object':{'sha':B}}, {'tree':{'sha':'tree'}},{'truncated':True,'tree':[]}]
        with self.assertRaises(ValueError):pipeline.current_source('owner/repo',A,Mock(side_effect=values))

    def test_superseded_preclaim_is_a_skip_without_writing_ownership(self):
        with patch.dict(os.environ,{'GITHUB_REPOSITORY':'owner/repo'},clear=True), \
             patch('sys.argv',['github_gate.py','--run-id','1','--expected-commit',A,'--expected-attempt','2','--check-only']), \
             patch.object(github_gate,'recheck_source',side_effect=github_gate.Superseded('new source')), \
             redirect_stdout(io.StringIO()) as output:
            github_gate.main()
        self.assertFalse(json.loads(output.getvalue())['eligible'])


class FrontendBuildTests(unittest.TestCase):
    def test_fresh_verifier_job_recreates_the_build_configuration(self):
        for event, config in [('pull_request', {'api_url':'https://ci.invalid/api/v1', 'base_path':'/ci/'}),
                              ('push', CONFIG)]:
            with self.subTest(event=event), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); producer = root/'producer'; (producer/'dist').mkdir(parents=True)
                (producer/'dist/index.html').write_text('built output')
                (producer/'dist/.nojekyll').write_bytes(b'')
                frontend_release.build_manifest(producer, commit=A, tree='tree', config=config, run_id=1, attempt=2)
                consumer = root/'downloaded'; shutil.copytree(producer, consumer)
                # A fresh acceptance runner has no VITE variables from the producer.
                env = {**ENV, 'GITHUB_EVENT_NAME':event, 'GITHUB_REPOSITORY':'owner/repo',
                       'GITHUB_ENV':str(root/'environment'), 'FRONTEND_API_URL':CONFIG['api_url'],
                       'FRONTEND_BASE_PATH':CONFIG['base_path']}
                with patch.dict(os.environ, env, clear=True), redirect_stdout(io.StringIO()):
                    pipeline.frontend_config_main()
                    configured = dict(line.split('=', 1) for line in (root/'environment').read_text().splitlines())
                    with patch.dict(os.environ, configured), patch.object(pipeline.subprocess, 'run', return_value=Mock(stdout='tree\n')):
                        pipeline.frontend_verify_main(directory=consumer)

    def test_downloaded_artifact_must_preserve_hidden_output_before_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'dist').mkdir()
            (root/'dist/index.html').write_text('checked build')
            hidden=root/'dist/.nojekyll';hidden.write_bytes(b'\n')
            env={**ENV,'VITE_API_BASE_URL':CONFIG['api_url'],'VITE_BASE_PATH':CONFIG['base_path']}
            frontend_release.build_manifest(root,commit=A,tree='tree',config=CONFIG,run_id=1,attempt=2)
            with patch.dict(os.environ,env,clear=True), \
                 patch.object(pipeline.subprocess,'run',return_value=Mock(stdout='tree\n')):
                pipeline.frontend_verify_main(directory=root)
                hidden.unlink()  # Simulate upload-artifact's default hidden-file exclusion.
                with self.assertRaisesRegex(ValueError,'files differ'):
                    pipeline.frontend_verify_main(directory=root)
            self.assertIn('dist/.nojekyll',json.loads((root/'frontend-build.json').read_text())['files'])

    def test_verified_files_config_and_attempt_must_be_the_ones_built(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'dist').mkdir();(root/'dist/index.html').write_text('checked build')
            args={'commit':A,'tree':'tree','config':CONFIG,'run_id':1,'attempt':2}
            frontend_release.build_manifest(root,**args)
            self.assertEqual(frontend_release.verify_build(root,**args)['commit'],A)
            for changes in ({'attempt':1},{'commit':B},{'config':{**CONFIG,'base_path':'/different/'}}):
                with self.subTest(changes=changes),self.assertRaises(ValueError):frontend_release.verify_build(root,**{**args,**changes})
            (root/'dist/index.html').write_text('changed after CI')
            with self.assertRaisesRegex(ValueError,'files differ'):frontend_release.verify_build(root,**args)

    def test_recursive_source_snapshot_excludes_test_only_changes(self):
        client=Mock()
        client.request.side_effect=[{'tree':{'sha':'tree'}},{'truncated':False,'tree':[
            {'path':'frontend','type':'tree','sha':'front'},
            {'path':'tests/test_any.py','type':'blob','sha':'test'},
            {'path':'backend/app/main.py','type':'blob','sha':'app'},
            {'path':'docs/index.html','type':'blob','sha':'html'}]}]
        snapshot=frontend_release.snapshot(client,A)
        self.assertEqual(snapshot['frontend'],'front')
        self.assertEqual(snapshot['backend'],{'backend/app/main.py':'app','docs/index.html':'html'})

    def test_artifact_publication_requires_ci_provenance_before_compatibility(self):
        client=Mock();client.repository='owner/repo'
        run={'id':1,'head_sha':A,'run_attempt':2,'event':'push','head_branch':'master','workflow_id':5,
             'head_repository':{'full_name':'owner/repo'}}
        for changes in ({'event':'pull_request'},{'head_sha':B},{'workflow_id':6},
                        {'head_repository':{'full_name':'other/repo'}},{'head_branch':'branch'}):
            client.request.side_effect=[{**run,**changes},{'id':5},{'default_branch':'master'}]
            with self.subTest(changes=changes),patch.object(ci_coverage,'check_run') as coverage, \
                 patch.object(frontend_release,'select') as select,self.assertRaises(ValueError):
                frontend_release.select_artifact(client,A,1,'.')
            coverage.assert_not_called();select.assert_not_called()

    @patch.dict(os.environ,{'FRONTEND_API_URL':CONFIG['api_url'],'FRONTEND_BASE_PATH':CONFIG['base_path']})
    def test_pages_retry_uses_the_checked_build_even_if_parent_run_failed_at_pages(self):
        client=Mock();client.repository='owner/repo'
        run={'id':1,'head_sha':A,'run_attempt':2,'event':'push','head_branch':'master','workflow_id':5,
             'status':'completed','conclusion':'failure','head_repository':{'full_name':'owner/repo'}}
        client.request.side_effect=[run,{'id':5},{'default_branch':'master'}]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'dist').mkdir();(root/'dist/index.html').write_text('checked')
            frontend_release.build_manifest(root,commit=A,tree='tree',config=CONFIG,run_id=1,attempt=2)
            with patch.object(ci_coverage,'check_run',return_value=proof_for(['frontend/src/main.jsx'],frontend=True)) as coverage, \
                 patch.object(frontend_release,'select',return_value={'publish':True}), \
                 patch.object(frontend_release,'snapshot',return_value={'frontend':'tree'}):
                self.assertTrue(frontend_release.select_artifact(client,A,1,root)['publish'])
            self.assertFalse(coverage.call_args.kwargs['require_backend'])


class DeferredRetryTests(unittest.TestCase):
    def test_exhausted_or_expired_retry_does_not_dispatch(self):
        state={'mode':'auto','blocked':False,'active':None}
        for pending in ({'retry_count':cd_pending.MAX_RETRIES}, {'expires_at':1}):
            api=Mock()
            with patch.object(cd_pending,'Store') as store,patch.object(cd_pending,'load',return_value=(pending,'sha')):
                store.return_value.load.return_value=(state,'sha')
                result=cd_pending.retry(api)
            self.assertFalse(result['retry_dispatched']);api.request.assert_not_called()

    def test_legacy_pending_record_remains_readable(self):
        import base64
        value={'commit':A,'ci_number':1,'ci_run_id':'1'}
        api=Mock();api.request.return_value={'type':'file','encoding':'base64','sha':'s',
                      'content':base64.b64encode(json.dumps(value).encode()).decode()}
        self.assertEqual(cd_pending.load(api)[0],value)

    def test_completed_release_clears_even_an_exhausted_old_pending(self):
        state={'mode':'auto','blocked':False,'active':None,'last_success':{'ci_number':2}}
        pending={'commit':A,'ci_number':1,'ci_run_id':'1','retry_count':cd_pending.MAX_RETRIES,'expires_at':1}
        api=Mock()
        with patch.object(cd_pending,'Store') as store,patch.object(cd_pending,'load',return_value=(pending,'s')), \
             patch.object(cd_pending,'save') as save:
            store.return_value.load.return_value=(state,'s')
            result=cd_pending.retry(api)
        self.assertFalse(result['retry_dispatched']);save.assert_called_once_with(api,None,completed_number=2)
        api.request.assert_not_called()

    def test_repeated_deferral_preserves_the_original_retry_budget(self):
        pending={'commit':A,'ci_number':1,'ci_run_id':'1','retry_count':3,'expires_at':2000}
        api=Mock()
        with patch.object(cd_pending,'load',return_value=(pending,'version')),patch.object(cd_pending.time,'time',return_value=1000):
            cd_pending.save(api,{k:pending[k] for k in ('commit','ci_number','ci_run_id')})
        api.request.assert_not_called()

    def test_superseded_deferred_source_never_dispatches(self):
        state={'mode':'auto','blocked':False,'active':None,'last_success':None}
        pending={'commit':A,'ci_number':1,'ci_run_id':'1'}
        api=Mock();api.repository='owner/repo'
        api.request.side_effect=lambda method,path,*args:{'value':'true'} if 'variables' in path else {'total_count':0}
        with patch.object(cd_pending,'Store') as store,patch.object(cd_pending,'load',return_value=(pending,'s')), \
             patch.object(cd_pending,'save') as save,patch.object(github_gate,'validate',side_effect=github_gate.Superseded('new source')):
            store.return_value.load.return_value=(state,'s')
            result=cd_pending.retry(api)
        self.assertFalse(result['retry_dispatched'])
        save.assert_called_once_with(api,None,completed_number=1)
        self.assertTrue(all(call.args[0]=='GET' for call in api.request.call_args_list))


if __name__=='__main__':unittest.main()
