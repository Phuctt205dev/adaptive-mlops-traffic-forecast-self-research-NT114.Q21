import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from pydantic import ValidationError
from backend.app.core.errors import ApplicationError
from backend.app.schemas.model_version import TrainingRunCreate
from backend.app.services import drift_monitoring, model_registry, training_execution


class KaggleOnlyPolicyTests(unittest.TestCase):
    def test_new_request_defaults_to_kaggle_and_rejects_local(self):
        self.assertEqual(TrainingRunCreate(train_start_date='2026-01-01', train_end_date='2026-02-01').training_backend, 'kaggle')
        with self.assertRaises(ValidationError):
            TrainingRunCreate(train_start_date='2026-01-01', train_end_date='2026-02-01', training_backend='local')

    def test_service_bypass_cannot_queue_or_create_local_execution(self):
        database = Mock()
        with self.assertRaises(ApplicationError):
            model_registry.create_queued_training_run(database, uuid.uuid4(), uuid.uuid4(), {'training_backend': 'local'}, 'synthetic')
        database.get.assert_not_called()
        with self.assertRaises(ApplicationError):
            training_execution.create_execution(database, SimpleNamespace(configuration_json={'training_backend': 'local'}))
        database.add.assert_not_called()

    def test_retrain_from_local_or_missing_backend_uses_new_kaggle_configuration(self):
        settings = SimpleNamespace(drift_retrain_window_months=24)
        for original in ({'training_backend': 'local', 'selected_models': ['lstm']}, {'selected_models': ['lstm']}):
            source = SimpleNamespace(configuration_json=original.copy())
            with patch.object(drift_monitoring, 'get_settings', return_value=settings):
                configuration = drift_monitoring._training_configuration(source, drift_monitoring.pd.Timestamp('2026-02-01'))
            self.assertEqual(configuration['training_backend'], 'kaggle')
            self.assertEqual(source.configuration_json, original)

    def test_fresh_api_process_has_no_training_routes_or_executor_imports(self):
        root = Path(__file__).resolve().parents[1]
        script = """import json,sys
from backend.app.main import app
from backend.app.api.routes.training_monitor import capabilities
from fastapi.testclient import TestClient
assert not any(p.startswith('/api/v1/internal/training-runs') for p in app.openapi()['paths'])
assert not any(name in sys.modules for name in ('src.pipeline','backend.app.api.routes.internal_training','backend.app.services.training_control'))
client=TestClient(app)
try: assert client.post('/api/v1/internal/training-runs/not-a-run/execute/tree').status_code==404
finally: client.close()
assert capabilities()['default_backend']=='kaggle'
print(json.dumps({'api_boundary_verified':True}))
"""
        environment = {**os.environ, 'DATABASE_URL': 'sqlite://', 'AWS_EC2_METADATA_DISABLED': 'true'}
        result = subprocess.run([sys.executable, '-B', '-c', script], cwd=root, env=environment,
                                text=True, encoding='utf-8', capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['api_boundary_verified'])


if __name__ == '__main__':
    unittest.main()
