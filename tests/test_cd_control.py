import base64
import copy
import json
import io
import os
import sys
import ssl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from scripts.cicd import cd_state as cd, diagnostics, github_gate, recover_cd, deploy
from tests import test_cicd as fixtures


class MemoryAPI:
    repository = 'owner/repo'
    def __init__(self, state=None):
        self.state = copy.deepcopy(state if state is not None else cd.initial())
        self.version, self.writes = 1, []
        self.conflict = None
        self.lost_reply = False

    def request(self, method, path, body=None):
        if method == 'GET':
            if path.startswith('actions/runs/'):
                return {'status': getattr(self, 'run_status', 'in_progress')}
            if self.state is None:
                raise cd.ApiError(404)
            return {'type': 'file', 'sha': f'{self.version:040x}', 'encoding': 'base64',
                    'content': base64.b64encode(json.dumps(self.state).encode()).decode()}
        if self.conflict:
            change, self.conflict = self.conflict, None
            change(self.state)
            self.version += 1
            raise cd.ApiError(409)
        if body['sha'] != f'{self.version:040x}':
            raise cd.ApiError(409)
        self.state = json.loads(base64.b64decode(body['content']))
        self.version += 1
        self.writes.append(copy.deepcopy(body))
        if self.lost_reply:
            self.lost_reply = False
            raise TimeoutError()
        return {}


def transaction(number=1, mode='manual', epoch=0):
    return {'run_id': str(number * 10), 'attempt': 1, 'commit': f'{number:040x}',
            'ci_number': number, 'mode': mode, 'epoch': epoch}


def receipt(item, *, rollback=False):
    return {'commit': item['commit'], 'guard_held': False, 'recovery_required': False,
            'completed': not rollback, 'rolled_back': rollback}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.api = MemoryAPI()
        self.store = cd.Store(self.api)

    def test_initial_manual_and_missing_configuration_never_enables_auto(self):
        self.api.state = None
        self.assertEqual(self.store.load()[0]['mode'], 'manual')
        with self.assertRaisesRegex(ValueError, 'not initialized'):
            cd.set_mode(self.store, 'auto', actor='owner', control_number=1)

    def test_one_auto_selection_survives_three_releases(self):
        cd.set_mode(self.store, 'auto', actor='owner', control_number=1)
        for number in range(1, 4):
            item = transaction(number, 'auto', 1)
            self.assertTrue(cd.begin(self.store, item)['began'])
            self.assertEqual(cd.finish(self.store, item, 'success', receipt(item), began=True)['outcome'], 'success')
            self.assertEqual(self.api.state['mode'], 'auto')
            self.assertIsNone(self.api.state['active'])
        self.assertTrue(all(write['branch'] == 'cd-settings' for write in self.api.writes))

    def test_mode_change_does_not_create_release_or_mutate_active_transaction(self):
        item = transaction()
        cd.begin(self.store, item)
        cd.set_mode(self.store, 'auto', actor='owner', control_number=1)
        self.assertEqual(self.api.state['active'], item)
        cd.finish(self.store, item, 'success', receipt(item), began=True)
        self.assertEqual(self.api.state['mode'], 'auto')

    def test_old_control_and_same_mode_do_not_rewrite_release_epoch(self):
        cd.set_mode(self.store, 'auto', actor='owner', control_number=2)
        before = copy.deepcopy(self.api.state)
        self.assertFalse(cd.set_mode(self.store, 'manual', actor='owner', control_number=1)['applied'])
        self.assertEqual(self.api.state, before)
        cd.set_mode(self.store, 'auto', actor='owner', control_number=3)
        self.assertEqual(self.api.state['epoch'], 1)

    def test_mode_changed_before_claim_skips_without_a_cluster_transaction(self):
        cd.set_mode(self.store, 'auto', actor='owner', control_number=1)
        result = cd.begin(self.store, transaction())
        self.assertFalse(result['began'])
        self.assertIsNone(self.api.state['active'])

    def test_failure_blocks_auto_and_manual_verified_repair_clears_it(self):
        cd.set_mode(self.store, 'auto', actor='owner', control_number=1)
        item = transaction(1, 'auto', 1)
        cd.begin(self.store, item)
        result = cd.finish(self.store, item, 'failure', receipt(item, rollback=True), began=True)
        self.assertTrue(result['blocked'])
        self.assertFalse(result['recovery_required'])
        with self.assertRaises(ValueError):
            cd.begin(self.store, transaction(2, 'auto', 1))
        with self.assertRaises(ValueError):
            cd.set_mode(self.store, 'auto', actor='owner', control_number=2)
        cd.set_mode(self.store, 'manual', actor='owner', control_number=2)
        repair = transaction(2, 'manual', 2)
        cd.begin(self.store, repair)
        cd.finish(self.store, repair, 'success', receipt(repair), began=True)
        self.assertFalse(self.api.state['blocked'])
        cd.set_mode(self.store, 'auto', actor='owner', control_number=3)

    def test_missing_or_foreign_receipt_retains_ownership_for_operator_recovery(self):
        for document in (None, receipt(transaction(2))):
            with self.subTest(receipt=document):
                self.setUp()
                item = transaction()
                cd.begin(self.store, item)
                result = cd.finish(self.store, item, 'failure', document, began=True)
                self.assertTrue(result['blocked'])
                self.assertTrue(result['recovery_required'])
                with self.assertRaisesRegex(ValueError, 'owns'):
                    cd.begin(self.store, transaction(2))

    def test_failure_before_claim_blocks_auto_without_claiming_cluster_recovery(self):
        result = cd.finish(self.store, transaction(), 'failure')
        self.assertTrue(result['blocked'])
        self.assertFalse(result['recovery_required'])

    def test_busy_preflight_receipt_proves_no_mutation_and_releases_ownership(self):
        item = transaction()
        cd.begin(self.store, item)
        document = {'commit': item['commit'], 'preflight_only': True, 'started': False,
                    'patched': [], 'guard_held': False, 'recovery_required': False}
        result = cd.finish(self.store, item, 'failure', document, began=True)
        self.assertTrue(result['blocked'])
        self.assertFalse(result['recovery_required'])
        self.assertIsNone(self.api.state['active'])

    def test_skipped_release_leaves_latch_and_state_untouched(self):
        before = copy.deepcopy(self.api.state)
        self.assertEqual(cd.finish(self.store, transaction(), 'success')['outcome'], 'skipped')
        self.assertEqual(before, self.api.state)

    def test_cancelled_obsolete_manual_request_does_not_block_new_auto_mode(self):
        cd.set_mode(self.store, 'auto', actor='owner', control_number=1)
        result = cd.finish(self.store, transaction(), 'cancelled', began=False)
        self.assertEqual(result['outcome'], 'obsolete unstarted release skipped')
        self.assertFalse(self.api.state['blocked'])

    def test_cancel_before_claim_does_not_create_a_false_deployment_failure(self):
        result = cd.finish(self.store, transaction(), 'cancelled')
        self.assertEqual(result['outcome'], 'unstarted cancellation skipped')
        self.assertFalse(self.api.state['blocked'])

    def test_unverified_outcome_fails_the_result_job_even_if_deploy_job_was_green(self):
        item = transaction()
        cd.begin(self.store, item)
        environment = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': item['run_id'],
            'GITHUB_RUN_ATTEMPT': '1', 'EXPECTED_COMMIT': item['commit'], 'CI_NUMBER': '1',
            'DELIVERY_MODE': 'manual', 'CONTROL_EPOCH': '0', 'DELIVERY_RESULT': 'success', 'DELIVERY_BEGAN': 'true'}
        with patch.dict(os.environ, environment, clear=True), patch.object(sys, 'argv', ['cd_state.py', 'finish']), \
             patch.object(cd, 'GitHub', return_value=self.api), patch.object(cd, 'Store', return_value=self.store), \
             patch.object(cd, 'read_receipt', return_value=None), patch.object(cd, 'emit'):
            self.assertEqual(cd.main(), 1)
        self.assertTrue(self.api.state['blocked'])

    def test_older_ci_cannot_overwrite_a_newer_success(self):
        item = transaction(3)
        cd.begin(self.store, item)
        cd.finish(self.store, item, 'success', receipt(item), began=True)
        self.assertFalse(cd.begin(self.store, transaction(1))['began'])

    def test_stale_success_cannot_clear_a_newer_failure(self):
        item = transaction(3)
        cd.finish(self.store, item, 'failure')
        result = cd.finish(self.store, transaction(), 'success', receipt(transaction()), began=True)
        self.assertEqual(result['outcome'], 'stale outcome ignored')
        self.assertTrue(self.api.state['blocked'])

    def test_cas_retries_preserve_a_concurrent_delivery_claim(self):
        item = transaction()
        self.api.conflict = lambda state: state.update(active=item)
        cd.set_mode(self.store, 'auto', actor='owner', control_number=1)
        self.assertEqual(self.api.state['active'], item)
        self.assertEqual(self.api.state['mode'], 'auto')

    def test_lost_write_response_is_confirmed_by_readback_without_another_write(self):
        self.api.lost_reply = True
        self.assertTrue(cd.set_mode(self.store, 'auto', actor='owner', control_number=1)['applied'])
        self.assertEqual(len(self.api.writes), 1)

    def test_invalid_configuration_and_actor_are_rejected(self):
        for field, value in (('mode', 'automatic'), ('blocked', 'false'), ('epoch', True),
                             ('updated_by', 'bad\nactor'), ('extra_secret', 'never allowed')):
            data = cd.initial(); data[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                cd.validate(data)

    def test_other_release_cannot_finish_an_owned_transaction(self):
        cd.begin(self.store, transaction(2))
        with self.assertRaises(ValueError):
            cd.finish(self.store, transaction(), 'failure')

    def test_malformed_receipt_cannot_clear_guard_ownership(self):
        item = transaction()
        cd.begin(self.store, item)
        document = receipt(item); document['guard_held'] = 'false'
        result = cd.finish(self.store, item, 'failure', document, began=True)
        self.assertTrue(result['recovery_required'])

    def test_completed_run_with_stranded_ownership_cannot_enable_auto(self):
        cd.begin(self.store, transaction())
        self.api.run_status = 'completed'
        with self.assertRaisesRegex(ValueError, 'ended without confirming'):
            cd.set_mode(self.store, 'auto', actor='owner', control_number=1)
        self.assertEqual(self.api.state['mode'], 'manual')


class APIAndBootstrapTests(unittest.TestCase):
    def test_root_endpoint_and_tls_verification_are_preserved(self):
        api = cd.GitHub('owner/repo', token='test-only-token')
        with patch.object(cd, 'urlopen', return_value=io.BytesIO(b'{"default_branch":"master"}')) as request:
            self.assertEqual(api.request('GET', '')['default_branch'], 'master')
        self.assertEqual(request.call_args.args[0].full_url, 'https://api.github.com/repos/owner/repo')
        context = request.call_args.kwargs['context']
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_http_error_never_exposes_response_or_credentials(self):
        api = cd.GitHub('owner/repo', token='test-only-token')
        error = HTTPError('https://api.github.com', 403, 'private-response-value', {}, None)
        with patch.object(cd, 'urlopen', side_effect=error):
            with self.assertRaises(cd.ApiError) as caught:
                api.request('GET', 'anything')
        self.assertNotIn('private-response-value', str(caught.exception))
        self.assertNotIn('test-only-token', str(caught.exception))

    def test_initialize_creates_only_configuration_ref_and_preserves_existing_auto(self):
        class Bootstrap(MemoryAPI):
            def __init__(self):
                super().__init__(); self.state = None; self.branch = False; self.refs = []
            def request(self, method, path, body=None):
                if method == 'GET' and path == '': return {'default_branch': 'master'}
                if path == 'git/ref/heads/master': return {'object': {'sha': 'a' * 40}}
                if path == 'git/ref/heads/cd-settings':
                    if not self.branch: raise cd.ApiError(404)
                    return {'object': {'sha': 'b' * 40}}
                if method == 'POST' and path == 'git/refs':
                    self.refs.append(body['ref']); self.branch = True; return {}
                if method == 'PUT' and self.state is None:
                    self.state = json.loads(base64.b64decode(body['content']))
                    self.writes.append(body); return {}
                return super().request(method, path, body)
        api = Bootstrap()
        self.assertEqual(cd.initialize(api)['mode'], 'manual')
        self.assertEqual(api.refs, ['refs/heads/cd-settings'])
        api.state['mode'] = 'auto'
        writes = len(api.writes)
        self.assertEqual(cd.initialize(api)['mode'], 'auto')
        self.assertEqual(len(api.writes), writes)


class PolicyTests(unittest.TestCase):
    def fetch(self, state, *, reviewers=False, branch='master'):
        values = iter([
            {'status': 'completed', 'conclusion': 'success', 'event': 'push', 'workflow_id': 1,
             'head_branch': 'master', 'head_sha': 'a' * 40, 'run_number': 10, 'id': 1, 'run_attempt': 1,
             'head_repository': {'full_name': 'owner/repo'}},
            {'id': 1}, {'default_branch': 'master'}, {'type': 'file'},
            {'type':'file','encoding':'base64','content':base64.b64encode(json.dumps({'schema':'traffic-runtime-contract/v1'}).encode()).decode()},
            {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(json.dumps(state).encode()).decode()},
            {'protection_rules': [{'type': 'required_reviewers', 'reviewers': [{'id': 1}]}] if reviewers else [],
             'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True}},
            {'branch_policies': [{'name': branch, 'type': 'branch'}]},
        ])
        return lambda path: next(values)

    def test_auto_selects_only_auto_environment(self):
        state = cd.initial(); state['mode'] = 'auto'
        result = github_gate.validate('owner/repo', '1', fetch=self.fetch(state), coverage=Mock())
        self.assertEqual(result['environment'], 'rke2-production-auto')

    def test_auto_rejects_unexpected_reviewers_and_broad_branch_policy(self):
        state = cd.initial(); state['mode'] = 'auto'
        for kwargs in ({'reviewers': True}, {'branch': '*'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                github_gate.validate('owner/repo', '1', fetch=self.fetch(state, **kwargs), coverage=Mock())

    def test_blocked_auto_never_reaches_a_runner(self):
        state = cd.initial(); state.update(mode='auto', blocked=True)
        with self.assertRaisesRegex(ValueError, 'blocked'):
            github_gate.validate('owner/repo', '1', fetch=self.fetch(state), coverage=Mock())


class DiagnosticTests(unittest.TestCase):
    def test_summary_distinguishes_intentional_skip_from_predeployment_failure(self):
        base = {'EXPECTED_COMMIT': 'a' * 40, 'GITHUB_RUN_ID': '10', 'GITHUB_RUN_ATTEMPT': '1'}
        for claimed, claim_outcome, expected in (('', 'failure', 'failure'), ('false', 'success', 'skipped'), ('true', 'success', 'failure')):
            with self.subTest(claimed=claimed, status=claim_outcome), \
                 patch.dict(os.environ, {**base, 'CLAIM_BEGAN': claimed, 'CLAIM_OUTCOME': claim_outcome}, clear=True), \
                 patch.object(sys, 'argv', ['diagnostics.py', '--outcome', 'skipped']), \
                 patch.object(diagnostics, 'report', return_value='safe summary') as render, patch('builtins.print'):
                diagnostics.main()
                self.assertEqual(render.call_args.args[-1], expected)

    def test_crashloop_rolls_back_keeps_failure_evidence_and_blocks_auto(self):
        helper = fixtures.DeliveryTests()
        self.addCleanup(helper.doCleanups)
        operator, values = helper.operator()
        before = copy.deepcopy(values)
        operator.path = operator.path.parent / ('delivery-' + fixtures.COMMIT + '-10-1.json')
        item = transaction(1, 'auto', 1); item['commit'] = fixtures.COMMIT
        api = MemoryAPI(); store = cd.Store(api)
        cd.set_mode(store, 'auto', actor='owner', control_number=1)
        cd.begin(store, item)
        waits = [0]
        def wait(name):
            waits[0] += 1
            if waits[0] == 1: raise RuntimeError('rollout failed')
        operator.cluster.wait.side_effect = wait
        bad = {'metadata': {'name': 'traffic-training-tree-failed'},
               'status': {'phase': 'Running', 'containerStatuses': [{'name': 'traffic-training-tree',
                'ready': False, 'restartCount': 2, 'state': {'waiting': {'reason': 'CrashLoopBackOff'}},
                'lastState': {'terminated': {'exitCode': 1}}}]}}
        operator.cluster.call.side_effect = lambda *args, **kwargs: {'items': [bad]} if args[0] == 'get' else 'ModuleNotFoundError: PRIVATE-TEST-VALUE\n'
        with self.assertRaisesRegex(RuntimeError, 'rollout failed'):
            operator.apply()
        self.assertEqual(values, before)
        self.assertTrue(operator.state['rolled_back'])
        self.assertTrue(cd.finish(store, item, 'failure', operator.state, began=True)['blocked'])
        summary = diagnostics.report(operator.path.parent, fixtures.COMMIT, '10', 1, 'failure')
        self.assertIn('Backend deployment: FAILED', summary)
        self.assertIn('CrashLoopBackOff', summary)
        self.assertIn('SUCCEEDED: previous specification restored', summary)
        self.assertNotIn('PRIVATE-TEST-VALUE', summary)
        self.assertIsNone(api.state['active'])

    def test_evidence_excludes_env_raw_exception_messages_and_full_paths(self):
        pod = {'metadata': {'name': 'traffic-api-bad'}, 'spec': {'env': {'PASSWORD': 'PRIVATE-VALUE'}},
               'status': {'phase': 'Running', 'containerStatuses': [{'name': 'traffic-api', 'ready': False,
                'restartCount': 2, 'state': {'waiting': {'reason': 'CrashLoopBackOff', 'message': 'PRIVATE-VALUE'}},
                'lastState': {'terminated': {'exitCode': 1}}}]}}
        cluster = Mock()
        cluster.call.side_effect = [{'items': [pod]},
            '  File "/private/user/main.py", line 42\nModuleNotFoundError: PRIVATE-VALUE\n',
            'ValueError: PRIVATE-VALUE\n']
        evidence = diagnostics.collect(cluster, ['traffic-api'])
        raw = json.dumps(evidence)
        self.assertNotIn('PRIVATE-VALUE', raw)
        self.assertNotIn('/private/user', raw)
        self.assertIn('ModuleNotFoundError', raw)
        self.assertIn('CrashLoopBackOff', raw)

    def test_collection_failure_is_reported_without_raising(self):
        cluster = Mock(); cluster.call.side_effect = RuntimeError('private provider data')
        self.assertEqual(diagnostics.collect(cluster, ['traffic-api'])['unavailable'], ['pod_status'])

    def test_rolled_back_release_is_still_reported_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            item = transaction()
            path = Path(directory)/('delivery-' + item['commit'] + '-10-1.json')
            path.write_text(json.dumps(receipt(item, rollback=True)))
            report = diagnostics.report(directory, item['commit'], '10', 1, 'failure')
            self.assertIn('Backend deployment: FAILED', report)
            self.assertIn('SUCCEEDED: previous specification restored', report)

    def test_summary_preserves_sanitized_business_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            item=transaction();value=receipt(item,rollback=True)
            value['probe_failure']={'target':'api','endpoint':'model-quality','http_status':503,
                                    'error_type':'ProbeFailure','raw':'PRIVATE-CREDENTIAL'}
            path=Path(directory)/('delivery-'+item['commit']+'-10-1.json')
            path.write_text(json.dumps(value))
            report=diagnostics.report(directory,item['commit'],'10',1,'failure')
            self.assertIn('model-quality',report);self.assertIn('HTTP 503',report)
            self.assertNotIn('PRIVATE-CREDENTIAL',report)


class RecoveryTests(unittest.TestCase):
    def setup_recovered(self):
        cluster = Mock(); cluster.namespace_uid = 'checked-namespace'
        values = {name: fixtures.deployment(name) for name in recover_cd.NAMES}
        pods = []
        for name, deployment in values.items():
            for index in range(deployment['spec'].get('replicas', 1)):
                pods.append({'metadata': {'name': name + '-' + str(index),
                             'labels': deployment['spec']['selector']['matchLabels']},
                             'status': {'phase': 'Running', 'containerStatuses': [{'ready': True}]}})
        cluster.call.side_effect = [{'metadata': {'uid': cluster.namespace_uid}}, {'items': pods}]
        cluster.deployment.side_effect = lambda name: values[name]
        cluster.remote.return_value = {'dag_paused': False, 'active_controlled_runs': 0, 'active_airflow_runs': 0}
        active = transaction()
        journal = {**receipt(active, rollback=True), 'dag_was_paused': False,
                   'plans': {name: {'uid': item['metadata']['uid'], 'before': deploy.spec_hash(item)} for name, item in values.items()}}
        return cluster, values, active, journal

    def test_closed_recovery_requires_exact_specs_all_ready_and_dag_state(self):
        cluster, _, active, journal = self.setup_recovered()
        proof = recover_cd.confirm(cluster, journal, active)
        api = MemoryAPI(); store = cd.Store(api)
        cd.begin(store, active)
        cd.finish(store, active, 'failure', None, began=True)
        result = recover_cd.release_ownership(store, copy.deepcopy(api.state['active']), proof)
        self.assertTrue(result['recovery_verified'])
        self.assertIsNone(api.state['active'])
        self.assertTrue(api.state['blocked'])

    def test_foreign_spec_and_unresolved_receipt_never_release_ownership(self):
        cluster, values, active, journal = self.setup_recovered()
        values['traffic-api']['spec']['replicas'] = 7
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            recover_cd.confirm(cluster, journal, active)
        cluster, _, active, journal = self.setup_recovered()
        cluster.remote.return_value['dag_paused'] = True
        with self.assertRaisesRegex(ValueError, 'DAG/training'):
            recover_cd.confirm(cluster, journal, active)

    def test_untrusted_proof_cannot_clear_a_stranded_transaction(self):
        api = MemoryAPI(); store = cd.Store(api)
        cd.begin(store, transaction())
        with self.assertRaises(ValueError):
            recover_cd.release_ownership(store, transaction(), {'recovery_verified': True})
        self.assertIsNotNone(api.state['active'])


if __name__ == '__main__':
    unittest.main()
