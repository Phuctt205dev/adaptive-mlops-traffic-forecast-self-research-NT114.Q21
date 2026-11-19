"""Input-upload failures must recover without a second notebook execution."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from src.cloud_training.kaggle_execution import KaggleExecution, ProviderUncertain, TrainingCancelled, initial_ledger
from src.cloud_training.kaggle_transport import ProviderError
from tests.test_kaggle_execution import FakeTransport


class InputRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'input').mkdir()
        context = {**{k: str(uuid.uuid4()) for k in ('run_id', 'region_id', 'dataset_id', 'attempt_id')},
                   'branch': 'tree', 'configuration_sha256': 'a' * 64}
        self.ledger = initial_ledger({'context': context, 'files': {}}, 'probeuser')
        self.saved, self.now, self.transport = [], [1000.0], FakeTransport()
        self.provider = self.make(self.ledger)

    def make(self, ledger):
        return KaggleExecution(self.transport, self.root, ledger,
                               lambda v: self.saved.append(copy.deepcopy(v)),
                               clock=lambda: self.now[0],
                               sleep=lambda seconds: self.now.__setitem__(0, self.now[0] + seconds),
                               background_progress=False)

    def count(self, operation):
        return sum(op == operation for op, _ in self.transport.calls)

    def fail_uploads(self, failures, *, accepted=False):
        original = self.transport.call
        self.uploads = []

        def call(operation, **values):
            if operation == 'dataset_create':
                self.uploads.append(json.loads((Path(values['directory']) / 'dataset-metadata.json').read_text()))
                self.assertTrue(self.saved[-1]['dataset_pending'])
                if len(self.uploads) <= failures:
                    self.transport.calls.append((operation, values))
                    if accepted:
                        self.transport.resources['dataset'] = True
                    raise ProviderError('reply unknown', operation=operation, category='connection_error')
            if operation == 'dataset_check' and not self.transport.resources['dataset']:
                self.transport.calls.append((operation, values))
                raise ProviderError('not found', operation=operation, category='http_error', http_status=404)
            return original(operation, **values)

        self.transport.call = call
        return call

    def test_lost_upload_reply_reads_existing_input_without_uploading_again(self):
        self.fail_uploads(1, accepted=True)
        self.assertTrue(self.provider.submit().endswith('/1'))
        self.assertEqual(self.count('dataset_create'), 1)
        self.assertEqual(self.count('kernel_push'), 1)
        self.assertEqual(self.transport.metadata['dataset_sources'], [self.ledger['dataset_reference'] + '/1'])

    def test_missing_input_retries_same_slug_after_confirmed_absence(self):
        self.fail_uploads(2)
        self.provider.submit()
        self.assertEqual(self.count('dataset_create'), 3)
        self.assertGreaterEqual(self.count('resource_exists'), 4)
        self.assertGreaterEqual(self.now[0], 1030)
        self.assertEqual({v['id'] for v in self.uploads}, {self.ledger['dataset_reference']})
        self.assertEqual(self.count('kernel_push'), 1)
        self.assertEqual(self.provider.ledger['dataset_upload_attempts'], 3)

    def test_exhausted_uploads_with_confirmed_absence_fail_without_notebook(self):
        self.fail_uploads(99)
        with self.assertRaises(ProviderError) as caught:
            self.provider.run(lambda event: None, lambda: False)
        self.assertNotIsInstance(caught.exception, ProviderUncertain)
        self.assertEqual(caught.exception.category, 'input_upload_failed')
        self.assertEqual(self.count('dataset_create'), 3)
        self.assertEqual(self.count('kernel_push'), 0)

    def test_unavailable_presence_read_never_permits_another_upload(self):
        original = self.fail_uploads(99)
        def call(operation, **values):
            if operation == 'resource_exists':
                self.transport.calls.append((operation, values))
                raise ProviderError('unknown', operation=operation, category='response_timeout')
            return original(operation, **values)
        self.transport.call = call
        with self.assertRaises(ProviderUncertain):
            self.provider.run(lambda event: None, lambda: False)
        self.assertEqual(self.count('dataset_create'), 1)
        self.assertEqual(self.count('kernel_push'), 0)
        self.assertEqual(self.provider.ledger['phase'], 'needs_attention')
        self.assertLessEqual(self.now[0], 1600)

    def test_delayed_visibility_is_observed_before_upload_retry(self):
        original = self.fail_uploads(1)
        def call(operation, **values):
            if operation == 'resource_exists' and self.now[0] >= 1005:
                self.transport.resources['dataset'] = True
            return original(operation, **values)
        self.transport.call = call
        self.provider.submit()
        self.assertEqual(self.count('dataset_create'), 1)
        self.assertEqual(self.count('kernel_push'), 1)

    def test_saved_pending_upload_reconciles_without_resetting_attempt_budget(self):
        self.fail_uploads(99)
        self.provider.save(dataset_pending=True, dataset_upload_attempts=3,
                           input_deadline=1600)
        with self.assertRaises(ProviderError) as caught:
            self.make(self.saved[-1]).submit()
        self.assertEqual(caught.exception.category, 'input_upload_failed')
        self.assertEqual(self.count('dataset_create'), 0)
        self.assertEqual(self.count('kernel_push'), 0)

    def test_input_is_never_recreated_after_notebook_may_have_started(self):
        self.fail_uploads(99)
        self.provider.save(dataset_pending=True, submit_pending=True)
        with self.assertRaises(ProviderUncertain):
            self.provider.submit()
        self.assertEqual(self.count('dataset_create'), 0)
        self.assertEqual(self.count('kernel_push'), 0)

    def test_cancel_during_reconciliation_prevents_upload_and_notebook_retry(self):
        self.fail_uploads(99)
        with self.assertRaises(TrainingCancelled):
            self.provider.submit(cancelled=lambda: self.now[0] >= 1002)
        self.assertEqual(self.count('dataset_create'), 1)
        self.assertEqual(self.count('kernel_push'), 0)
        self.assertLessEqual(self.now[0], 1002)

    def test_successful_upload_with_transient_read_failure_only_retries_reads(self):
        original = self.transport.call
        failures = [2]
        def call(operation, **values):
            if operation == 'dataset_check' and failures[0]:
                failures[0] -= 1
                self.transport.calls.append((operation, values))
                raise ProviderError('read unavailable', operation=operation, category='response_timeout')
            return original(operation, **values)
        self.transport.call = call
        self.provider.submit()
        self.assertEqual(self.count('dataset_create'), 1)
        self.assertEqual(self.count('kernel_push'), 1)

    def test_definitive_auth_failure_is_not_retried(self):
        original = self.transport.call
        def call(operation, **values):
            if operation == 'dataset_create':
                self.transport.calls.append((operation, values))
                raise ProviderError('forbidden', operation=operation, category='http_error', http_status=403)
            return original(operation, **values)
        self.transport.call = call
        with self.assertRaises(ProviderError) as caught:
            self.provider.submit()
        self.assertEqual(caught.exception.http_status, 403)
        self.assertEqual(self.count('dataset_create'), 1)
        self.assertEqual(self.count('kernel_push'), 0)

    def test_confirmed_upload_is_not_repeated_while_metadata_is_invisible(self):
        original = self.transport.call
        def call(operation, **values):
            if operation == 'dataset_check':
                self.transport.calls.append((operation, values))
                raise ProviderError('invisible', operation=operation, category='http_error', http_status=404)
            if operation == 'resource_exists':
                self.transport.calls.append((operation, values))
                return {'exists': False}
            return original(operation, **values)
        self.transport.call = call
        with self.assertRaises(ProviderUncertain):
            self.provider.submit()
        self.assertEqual(self.count('dataset_create'), 1)
        self.assertEqual(self.count('kernel_push'), 0)

    def test_existing_notebook_blocks_input_retry_even_with_missing_dataset(self):
        original = self.fail_uploads(99)
        def call(operation, **values):
            if operation == 'resource_exists' and values['kind'] == 'notebook':
                self.transport.calls.append((operation, values))
                return {'exists': True}
            return original(operation, **values)
        self.transport.call = call
        with self.assertRaises(ProviderUncertain):
            self.provider.submit()
        self.assertEqual(self.count('dataset_create'), 1)
        self.assertEqual(self.count('kernel_push'), 0)

    def test_notebook_presence_timeout_retries_reads_before_upload(self):
        original = self.fail_uploads(1)
        failed = [False]
        def call(operation, **values):
            if operation == 'resource_exists' and values['kind'] == 'notebook' and not failed[0]:
                failed[0] = True
                self.transport.calls.append((operation, values))
                raise ProviderError('unknown', operation=operation, category='response_timeout')
            return original(operation, **values)
        self.transport.call = call
        self.provider.submit()
        self.assertGreaterEqual(self.now[0], 1030)
        self.assertEqual(self.count('dataset_create'), 2)
        self.assertEqual(self.count('kernel_push'), 1)

    def test_invalid_presence_is_never_treated_as_confirmed_absence(self):
        original = self.fail_uploads(99)
        def call(operation, **values):
            if operation == 'resource_exists':
                self.transport.calls.append((operation, values))
                return {'exists': 0}
            return original(operation, **values)
        self.transport.call = call
        with self.assertRaises(ProviderUncertain):
            self.provider.submit()
        self.assertEqual(self.count('dataset_create'), 1)
        self.assertEqual(self.count('kernel_push'), 0)

    def test_empty_notebook_presence_does_not_authorize_upload_or_busy_loop(self):
        original = self.fail_uploads(99)
        def call(operation, **values):
            if operation == 'resource_exists' and values['kind'] == 'notebook':
                return {}
            return original(operation, **values)
        self.transport.call = call
        with self.assertRaises(ProviderUncertain):
            self.provider.submit()
        self.assertEqual(self.count('dataset_create'), 1)
        self.assertEqual(self.count('kernel_push'), 0)

    def test_deadline_expiring_during_readback_prevents_upload(self):
        original = self.fail_uploads(99)
        def call(operation, **values):
            if operation == 'resource_exists' and values['kind'] == 'notebook':
                self.now[0] = 1600
            return original(operation, **values)
        self.transport.call = call
        with self.assertRaises(ProviderUncertain):
            self.provider.submit()
        self.assertEqual(self.count('dataset_create'), 1)
        self.assertEqual(self.count('kernel_push'), 0)

    def test_expired_saved_deadline_cannot_be_reset_by_a_new_submit_call(self):
        self.provider.save(dataset_pending=True, dataset_upload_attempts=1, input_deadline=999)
        with self.assertRaises(ProviderUncertain):
            self.make(self.saved[-1]).submit()
        self.assertFalse(self.transport.calls)
        self.assertEqual(self.saved[-1]['input_deadline'], 999)

    def test_private_version_guard_survives_upload_recovery(self):
        for private, version in [(False, 1), (True, 2)]:
            with self.subTest(private=private, version=version):
                self.transport = FakeTransport()
                self.provider = self.make(self.ledger)
                original = self.fail_uploads(1, accepted=True)
                def call(operation, **values):
                    if operation == 'dataset_check':
                        return {'private': private, 'version': version, 'status': 'ready'}
                    return original(operation, **values)
                self.transport.call = call
                with self.assertRaises(ProviderError):
                    self.provider.submit()
                self.assertEqual(self.count('kernel_push'), 0)

    def test_conflicting_create_reply_reads_the_same_dataset_and_starts_once(self):
        original = self.fail_uploads(1)
        def call(operation, **values):
            if operation == 'dataset_create' and self.count('dataset_create') == 1:
                self.transport.calls.append((operation, values))
                self.transport.resources['dataset'] = True
                raise ProviderError('already exists', operation=operation, category='http_error', http_status=409)
            return original(operation, **values)
        self.transport.call = call
        self.provider.submit()
        self.assertEqual(self.count('dataset_create'), 2)
        self.assertEqual(self.count('kernel_push'), 1)


if __name__ == '__main__':
    unittest.main()
