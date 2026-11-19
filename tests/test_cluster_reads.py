"""Transport faults must not replay writes or hide unsafe/permanent failures."""
import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.cicd.deploy import Cluster, ClusterOperationError
from scripts.cicd import roles_deploy


class ClusterReadTests(unittest.TestCase):
    def setUp(self):
        self.cluster = object.__new__(Cluster)
        self.cluster.prefix = ['kubectl', '--kubeconfig', 'private-profile']
        self.clock = 0.0

    def sleep(self, seconds):
        self.clock += seconds

    def response(self, code=0, stderr='', stdout='{"items": []}'):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def run_call(self, responses, *arguments, **kwargs):
        with patch('scripts.cicd.deploy.subprocess.run', side_effect=responses) as run, \
             patch('scripts.cicd.deploy.time.monotonic', side_effect=lambda: self.clock), \
             patch('scripts.cicd.deploy.time.sleep', side_effect=self.sleep) as sleep:
            result = self.cluster.call(*arguments, **kwargs)
            return result, run, sleep

    def test_transient_read_recovers_without_exposing_stderr(self):
        result, run, sleep = self.run_call([
            self.response(1, 'Unable to connect to the server: TLS handshake timeout private-token'),
            self.response(),
        ], 'get', 'pods', '-o', 'json')
        self.assertEqual(result, {'items': []})
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_permanent_error_has_safe_target_and_no_retry(self):
        for stderr, category in [('Error from server (Forbidden): private-token', 'forbidden'),
                                 ('Error from server (Unauthorized): private-token', 'unauthorized'),
                                 ('Error from server (NotFound): private-token', 'not_found'),
                                 ('x509: certificate signed by unknown authority private-token', 'tls_verification'),
                                 ('unrecognized failure private-token', 'unknown')]:
            with self.subTest(category=category), \
                 patch('scripts.cicd.deploy.subprocess.run', return_value=self.response(1, stderr)) as run, \
                 patch('scripts.cicd.deploy.time.sleep') as sleep:
                with self.assertRaises(RuntimeError) as caught:
                    self.cluster.call('get', 'deployment', 'api-next', '-o', 'json')
                self.assertEqual(run.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(caught.exception.facts['category'], category)
                self.assertEqual(caught.exception.facts['name'], 'api-next')
                self.assertNotIn('private-token', str(caught.exception))
                self.assertNotIn('private-profile', str(caught.exception))

    def test_writes_and_exec_are_never_replayed_after_transport_error(self):
        for operation in ('patch', 'create', 'delete', 'exec', 'rollout'):
            with self.subTest(operation=operation), \
                 patch('scripts.cicd.deploy.subprocess.run', return_value=self.response(1, 'connection reset by peer')) as run:
                with self.assertRaises(RuntimeError):
                    self.cluster.call(operation, 'deployment', 'api')
                self.assertEqual(run.call_count, 1)

    def test_transient_reads_exhaust_three_attempts(self):
        with patch('scripts.cicd.deploy.subprocess.run', return_value=self.response(1, 'connection reset by peer')) as run, \
             patch('scripts.cicd.deploy.time.monotonic', side_effect=lambda: self.clock), \
             patch('scripts.cicd.deploy.time.sleep', side_effect=self.sleep):
            with self.assertRaises(RuntimeError) as caught:
                self.cluster.call('get', 'pods', '-o', 'json')
        self.assertEqual(run.call_count, 3)
        self.assertEqual(caught.exception.facts['attempts'], 3)
        self.assertEqual(caught.exception.facts['category'], 'connection')

    def test_retry_respects_original_total_timeout_budget(self):
        def failure(*args, **kwargs):
            self.clock += 2.5
            return self.response(1, 'TLS handshake timeout')
        with patch('scripts.cicd.deploy.subprocess.run', side_effect=failure) as run, \
             patch('scripts.cicd.deploy.time.monotonic', side_effect=lambda: self.clock), \
             patch('scripts.cicd.deploy.time.sleep', side_effect=self.sleep) as sleep:
            with self.assertRaises(RuntimeError):
                self.cluster.call('get', 'pods', '-o', 'json', timeout=3)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

    def test_each_retry_uses_only_the_remaining_budget(self):
        def failure_then_success(*args, **kwargs):
            self.clock += 2
            return self.response(1, 'TooManyRequests') if self.clock == 2 else self.response()
        with patch('scripts.cicd.deploy.subprocess.run', side_effect=failure_then_success) as run, \
             patch('scripts.cicd.deploy.time.monotonic', side_effect=lambda: self.clock), \
             patch('scripts.cicd.deploy.time.sleep', side_effect=self.sleep):
            self.cluster.call('get', 'pods', '-o', 'json', timeout=10)
        self.assertEqual([c.kwargs['timeout'] for c in run.call_args_list], [10, 7])

    def test_raw_proxy_and_watch_requests_are_not_replayed(self):
        for arguments in [('get', '--raw', '/private'), ('get', 'pods', '--watch=true'),
                          ('get', 'pods', '--raw=/private'), ('get', 'pods', '-w')]:
            with self.subTest(arguments=arguments), \
                 patch('scripts.cicd.deploy.subprocess.run', return_value=self.response(1, 'connection reset')) as run:
                with self.assertRaises(ClusterOperationError):
                    self.cluster.call(*arguments)
                self.assertEqual(run.call_count, 1)

    def test_invalid_json_and_oversized_output_are_not_retried(self):
        for output, category in [('not-json', 'invalid_json'), ('x' * (8 * 1024**2 + 1), 'response_budget')]:
            with self.subTest(category=category), \
                 patch('scripts.cicd.deploy.subprocess.run', return_value=self.response(stdout=output)) as run:
                with self.assertRaises(RuntimeError) as caught:
                    self.cluster.call('get', 'pods', '-o', 'json')
                self.assertEqual(run.call_count, 1)
                self.assertEqual(caught.exception.facts['category'], category)

    def test_process_timeout_is_sanitized_without_replaying_write(self):
        failure = subprocess.TimeoutExpired(['kubectl', 'private-token'], 100, stderr='private-token')
        with patch('scripts.cicd.deploy.subprocess.run', side_effect=failure) as run:
            with self.assertRaises(RuntimeError) as caught:
                self.cluster.call('patch', 'deployment', 'api')
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('private-token', str(caught.exception))
        self.assertEqual(caught.exception.facts['category'], 'process_timeout')

    def test_process_timeout_cannot_restart_an_expired_read_budget(self):
        def expired(*args, **kwargs):
            self.clock += kwargs['timeout']
            raise subprocess.TimeoutExpired(['private-token'], kwargs['timeout'])
        with patch('scripts.cicd.deploy.subprocess.run', side_effect=expired) as run, \
             patch('scripts.cicd.deploy.time.monotonic', side_effect=lambda: self.clock), \
             patch('scripts.cicd.deploy.time.sleep') as sleep:
            with self.assertRaises(ClusterOperationError):
                self.cluster.call('get', 'pods', timeout=4)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

    def test_safe_failure_facts_survive_successful_rollback_in_receipt(self):
        error = ClusterOperationError(('get', 'deployment', 'api-next'), 'timeout', 3)
        with tempfile.TemporaryDirectory() as directory:
            op = object.__new__(roles_deploy.RolesDelivery)
            op.path = Path(directory) / 'receipt.json'
            op.state = {'phase': 'rolled_back', 'failed_phase': 'retire', 'rolled_back': True,
                        'guard_held': False, 'recovery_required': False}
            op.apply = Mock(side_effect=error)
            argv = ['roles_deploy.py', 'Apply']
            for name in ('directory', 'binding', 'commit', 'journal', 'kubectl', 'context', 'namespace-uid', 'kubeconfig'):
                argv.extend(['--' + name, 'private-profile'])
            with patch('sys.argv', argv), patch.object(roles_deploy, 'Cluster'), \
                 patch.object(roles_deploy, 'RolesDelivery', return_value=op), redirect_stdout(io.StringIO()) as output:
                status = roles_deploy.main()
            self.assertEqual(status, 1)
            receipt = json.loads(op.path.read_text())
            self.assertEqual(receipt['cluster_failure'], error.facts)
            self.assertEqual(receipt['failed_phase'], 'retire')
            self.assertTrue(receipt['rolled_back'])
            self.assertFalse(receipt['recovery_required'])
            self.assertNotIn('private-profile', output.getvalue() + op.path.read_text())


if __name__ == '__main__':
    unittest.main()
