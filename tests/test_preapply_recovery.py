"""Recovery must prove pre-engine failure AND unchanged last verified release."""
import base64
import copy
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import zipfile

from scripts.cicd import cd_state, recover_cd, recover_preapply as recovery
from scripts.cicd.prepare_artifact import Deadline
from tests.test_cd_control import MemoryAPI, transaction


class EvidenceClient:
    repository = 'owner/repo'

    def __init__(self):
        self.active = {**transaction(2, 'auto'), 'recovery_required': True}
        self.previous = transaction(1, 'auto')
        self.state = {**cd_state.initial(), 'mode': 'auto', 'blocked': True,
                      'active': self.active, 'last_success': self.previous, 'failure': copy.deepcopy(self.active)}
        self.deadline = Deadline()
        self.runs = {t['run_id']: {'id': int(t['run_id']), 'run_attempt': t['attempt'], 'head_sha': t['commit'],
                     'status': 'completed', 'conclusion': result, 'event': 'workflow_run',
                     'path': '.github/workflows/backend-deploy.yml', 'head_repository': {'full_name': self.repository}}
                     for t, result in [(self.active, 'failure'), (self.previous, 'success')]}
        self.jobs = [{'name': name, 'id': i, 'status': 'completed', 'conclusion': result}
                     for i, (name, result) in enumerate([('inspect', 'success'), ('deploy', 'failure'), ('record-result', 'failure')], 1)]
        self.jobs[1]['steps'] = [{'name': name, 'conclusion': result, 'status': 'completed'} for name, result in [
            ('Claim this deployment under the saved mode', 'success'), ('Deliver the approved immutable release', 'failure')]]
        self.log = (f"2026-11-09T00:00:00Z {self.active['commit']}\n"
                    '{"began": true}\nValueError: Role release files differ\n'
                    'Release file verification failed.\ndeliver.ps1:28 char:28\n').encode()
        self.source = b'reviewed controller'
        self.failed_artifacts = []
        self.journal = {'schema': 'traffic-role-delivery/v2', 'commit': self.previous['commit'],
                        'completed': True, 'rolled_back': False, 'guard_held': False, 'recovery_required': False,
                        'resources': [{'kind': kind, 'name': n} for kind, names in
                                      [('deployment', recovery.DEPLOYMENTS), ('service', recovery.SERVICES)] for n in names]}
        self.refresh_archive()

    def refresh_archive(self):
        previous = self.previous
        stream = io.BytesIO()
        name = f'delivery-{previous["commit"]}-{previous["run_id"]}-{previous["attempt"]}.json'
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr(name, json.dumps(self.journal))
        self.blob = stream.getvalue()
        self.item = {'name': f'delivery-receipt-{previous["commit"]}-{previous["attempt"]}', 'id': 12,
                     'expired': False, 'size_in_bytes': len(self.blob), 'digest': 'sha256:' + recovery.sha(self.blob),
                     'workflow_run': {'id': int(previous['run_id']), 'head_sha': previous['commit']}}

    def get(self, path):
        if path.startswith('contents/'):
            return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(self.source).decode()}
        if '/jobs?' in path:
            return {'total_count': len(self.jobs), 'jobs': self.jobs}
        if '/artifacts?' in path:
            items = [self.item] if '/10/' in path else self.failed_artifacts
            return {'total_count': len(items), 'artifacts': items}
        return self.runs[path.split('/')[-1]]

    def open(self, path):
        return io.BytesIO(self.log)

    def archive(self, artifact_id):
        return io.BytesIO(self.blob)


class PreapplyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = EvidenceClient()
        self.controllers = patch.dict(recovery.CONTROLLERS, {p: recovery.sha(self.client.source) for p in recovery.CONTROLLERS})
        self.controllers.start()
        self.addCleanup(self.controllers.stop)
        self.cluster = Mock(namespace_uid='reviewed-namespace')
        self.cluster.call.return_value = {'items': []}

    def test_proof_binds_real_zip_and_original_failed_transaction(self):
        with patch.object(recovery, 'confirm_roles', return_value={'recovery_verified': True, 'deployments_ready': 5}) as confirm:
            proof = recovery.prove(self.client, self.cluster, self.client.state)
        self.assertEqual(proof['active'], self.client.active)
        self.assertEqual(proof['baseline_receipt_sha256'], recovery.sha(json.dumps(self.client.journal).encode()))
        self.assertEqual(confirm.call_args.args[1]['commit'], self.client.previous['commit'])
        self.assertNotEqual(confirm.call_args.args[1]['commit'], self.client.active['commit'])
        self.assertTrue(proof['engine_not_entered'])

    def test_run_identity_never_accepts_other_attempt_or_nonterminal_failure(self):
        run = self.client.runs['20']
        for field, value in [('run_attempt', 2), ('head_sha', 'f' * 40), ('status', 'in_progress'),
                             ('conclusion', 'success'), ('event', 'pull_request'), ('path', 'other.yml')]:
            with self.subTest(field=field), patch.dict(run, {field: value}), self.assertRaises(ValueError):
                recovery.prove_failure(self.client, self.client.active)

    def test_unknown_original_controller_cannot_use_this_adapter(self):
        self.client.source = b'different implementation with a later throw'
        with self.assertRaisesRegex(ValueError, 'Unknown original controller'):
            recovery.prove_failure(self.client, self.client.active)

    def test_missing_claim_or_running_job_cannot_be_recovered(self):
        for change in ['claim', 'running', 'extra']:
            with self.subTest(change=change):
                client = EvidenceClient()
                if change == 'claim':
                    client.jobs[1]['steps'][0]['conclusion'] = 'failure'
                elif change == 'running':
                    client.jobs[2]['status'] = 'in_progress'
                else:
                    client.jobs.append({'name': 'extra', 'status': 'completed'})
                with self.assertRaises(ValueError):
                    recovery.prove_failure(client, client.active)

    def test_absent_or_ambiguous_preengine_failure_is_rejected(self):
        original = self.client.log
        for log in [b'', original.replace(self.client.active['commit'].encode(), b'f' * 40),
                    original.replace(b'28 char:28', b'49 char:28'), original.replace(b'"began": true', b'"began": false'),
                    original + b'{"delivery_phase":"admission"}', original + b'roles_deploy.py']:
            with self.subTest(log=log[-50:]), self.assertRaises(ValueError):
                self.client.log = log
                recovery.prove_failure(self.client, self.client.active)

    def test_existing_failed_run_receipt_requires_original_receipt_recovery(self):
        self.client.failed_artifacts = [{'name': 'delivery-receipt-original'}]
        with self.assertRaisesRegex(ValueError, 'original receipt recovery'):
            recovery.prove_failure(self.client, self.client.active)

    def test_baseline_requires_complete_successful_receipt(self):
        for change in ['subset', 'duplicate', 'commit', 'incomplete', 'guard', 'recovery', 'rollback']:
            with self.subTest(change=change):
                client = EvidenceClient()
                if change == 'subset':
                    client.journal['resources'].pop()
                elif change == 'duplicate':
                    client.journal['resources'][0] = client.journal['resources'][1]
                else:
                    key, value = {'commit': ('commit', 'b' * 40), 'incomplete': ('completed', False),
                                  'guard': ('guard_held', True), 'recovery': ('recovery_required', True),
                                  'rollback': ('rolled_back', True)}[change]
                    client.journal[key] = value
                client.refresh_archive()
                with self.assertRaises(ValueError):
                    recovery.baseline_receipt(client, client.previous)

    def test_corrupted_original_zip_cannot_prove_recovery(self):
        self.client.blob = b'not the original ZIP'
        with self.assertRaises(Exception):
            recovery.baseline_receipt(self.client, self.client.previous)

    def test_expired_or_foreign_baseline_artifact_is_rejected(self):
        for field, value in [('expired', True), ('digest', ''), ('workflow_run', {'id': 99, 'head_sha': 'b' * 40})]:
            with self.subTest(field=field), patch.dict(self.client.item, {field: value}), self.assertRaises(ValueError):
                recovery.baseline_receipt(self.client, self.client.previous)

    def test_changed_cluster_or_active_guard_stops_before_any_state_write(self):
        with patch.object(recovery, 'confirm_roles', side_effect=ValueError('Resource differs')), self.assertRaises(ValueError):
            recovery.prove(self.client, self.cluster, self.client.state)
        self.cluster.call.return_value = {'items': [{'metadata': {'name': 'delivery-other'}, 'status': {}}]}
        with patch.object(recovery, 'confirm_roles', return_value={}), self.assertRaisesRegex(ValueError, 'admission job'):
            recovery.prove(self.client, self.cluster, self.client.state)

    def test_rerun_during_cluster_check_is_rejected(self):
        def cluster_check(*args):
            self.client.runs['20']['run_attempt'] = 2
            return {}
        with patch.object(recovery, 'confirm_roles', side_effect=cluster_check), self.assertRaises(ValueError):
            recovery.prove(self.client, self.cluster, self.client.state)

    def test_orphan_guard_pod_also_prevents_recovery(self):
        self.cluster.call.side_effect = [
            {'items': []}, {'items': [{'metadata': {'name': 'delivery-orphan'}, 'status': {'phase': 'Running'}}]}]
        with patch.object(recovery, 'confirm_roles', return_value={}), self.assertRaisesRegex(ValueError, 'admission pod'):
            recovery.prove(self.client, self.cluster, self.client.state)

    def test_successful_release_keeps_auto_blocked_and_same_baseline(self):
        api = MemoryAPI(self.client.state)
        proof = {'recovery_verified': True, 'deployments_ready': 5}
        recover_cd.release_ownership(cd_state.Store(api), self.client.active, proof, expected_success=self.client.previous)
        self.assertIsNone(api.state['active'])
        self.assertTrue(api.state['blocked'])
        self.assertEqual(api.state['mode'], 'auto')
        self.assertEqual(api.state['last_success'], self.client.previous)
        self.assertFalse(api.state['failure']['recovery_required'])

    def test_compare_and_swap_refuses_changed_owner_or_baseline(self):
        for field in ['active', 'last_success']:
            with self.subTest(field=field):
                api = MemoryAPI(self.client.state)
                api.conflict = lambda state: state.update({field: transaction(3)})
                with self.assertRaises(ValueError):
                    recover_cd.release_ownership(cd_state.Store(api), self.client.active,
                        {'recovery_verified': True, 'deployments_ready': 5}, expected_success=self.client.previous)
                self.assertFalse(api.writes)

    def test_cli_defaults_to_proof_only_without_releasing_ownership(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'GITHUB_REPOSITORY': 'owner/repo', 'GH_TOKEN': 'test'}), \
             patch('sys.argv', ['recover_preapply.py', '--kubectl', 'kubectl', '--kubeconfig', 'config', '--context', 'ctx',
                               '--namespace-uid', 'uid', '--proof-output', str(Path(directory) / 'proof.json')]), \
             patch.object(recovery, 'Store') as store, patch.object(recovery, 'GitHub'), \
             patch.object(recovery, 'GitHubArtifactClient'), patch.object(recovery, 'Cluster'), \
             patch.object(recovery, 'prove', return_value={'recovery_verified': True}), patch.object(recovery, 'release_ownership') as release:
            store.return_value.load.return_value = (self.client.state, 'version')
            recovery.main()
            release.assert_not_called()
            self.assertTrue((Path(directory) / 'proof.json').is_file())


if __name__ == '__main__':
    unittest.main()
