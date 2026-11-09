"""Status reconciliation uses bounded authenticated GETs, never a DAG trigger."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.app.core.errors import ApplicationError
from backend.app.services import airflow_client


class AirflowStatusReadTests(unittest.TestCase):
    def read(self, document=None, *, status=200, chunks=None, clock=None):
        response = Mock(status_code=status)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = chunks if chunks is not None else [json.dumps(document).encode()]
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.return_value = response
        settings = SimpleNamespace(airflow_api_url='http://airflow/api/v1', airflow_admin_username='synthetic',
                                   airflow_admin_password='not-a-real-secret')
        with patch.object(airflow_client.requests, 'Session', return_value=session), \
             patch.object(airflow_client, 'get_settings', return_value=settings), \
             patch.object(airflow_client.time, 'monotonic', side_effect=clock or [0, 1, 2, 3]):
            try:
                return airflow_client.get_dag_run('training', 'run/one')
            finally:
                self.assertFalse(session.trust_env)
                session.post.assert_not_called()
                self.assertTrue(session.get.call_args.kwargs['stream'])
                self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
                self.assertIn('/run%2Fone', session.get.call_args.args[0])
                response.__exit__.assert_called_once()

    def test_exact_identity_is_required(self):
        expected = {'dag_id': 'training', 'dag_run_id': 'run/one', 'state': 'failed'}
        self.assertEqual(self.read(expected), expected)
        with self.assertRaises(ValueError): self.read({**expected, 'dag_run_id': 'another-run'})

    def test_non_success_or_redirect_does_not_use_a_response_as_evidence(self):
        for status in [302, 401, 404, 503]:
            with self.subTest(status=status), self.assertRaises(ApplicationError):
                self.read(status=status)

    def test_oversized_or_malformed_response_is_rejected(self):
        for chunks in [[b'x' * (128 * 1024 + 1)], [b'not JSON']]:
            with self.subTest(size=len(chunks[0])), self.assertRaises(ValueError): self.read(chunks=chunks)

    def test_slow_stream_exceeds_total_budget(self):
        with self.assertRaises(TimeoutError): self.read(chunks=[b'{}'], clock=[0, 7])
