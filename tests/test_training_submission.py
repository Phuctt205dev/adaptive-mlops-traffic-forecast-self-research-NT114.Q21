"""Admin HTTP retries reuse one durable run, without repeated Airflow dispatch."""
from unittest.mock import patch
import unittest
import uuid
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from backend.app.api.dependencies import require_admin
from backend.app.api.routes import datasets
from backend.app.core.config import Settings
from backend.app.core.errors import register_exception_handlers
from backend.app.db.models import Base, Dataset, DatasetStatus, Region, TrainingRun, TrainingRunStatus, User, UserRole
from backend.app.db.session import get_db
from backend.app.db.training_execution import TrainingExecution


class TrainingSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.admin = User(email="submit@example.test", password_hash="synthetic", role=UserRole.ADMIN)
        region = Region(name="Synthetic", latitude="10", longitude="106", timezone="UTC")
        self.db.add_all([self.admin, region]); self.db.flush()
        self.dataset = Dataset(region_id=region.id, uploaded_by=self.admin.id, original_filename="synthetic.csv",
            contract_version="traffic-training-csv/v1", storage_uri="s3://synthetic/input", sha256="a" * 64,
            row_count=5000, status=DatasetStatus.VALID)
        self.db.add(self.dataset); self.db.commit()
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(datasets.router, prefix="/api/v1/admin")
        app.dependency_overrides[require_admin] = lambda: self.admin
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.settings = Settings(_env_file=None, training_control_enabled=True, cloud_training_enabled=True)
        self.payload = {"request_id": str(uuid.uuid4()), "training_backend": "kaggle",
                        "train_start_date": "2026-01-01", "train_end_date": "2026-11-01"}
        self.url = f"/api/v1/admin/datasets/{self.dataset.id}/training-runs"

    def test_lost_success_response_can_be_retried_without_another_dispatch(self):
        with patch.object(datasets, "get_settings", return_value=self.settings), \
             patch.object(datasets.airflow_client, "trigger_dag", return_value={}) as trigger:
            first = self.client.post(self.url, json=self.payload)
            second = self.client.post(self.url, json=self.payload)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(first.json()["training_run"]["id"], second.json()["training_run"]["id"])
        self.assertTrue(second.json()["airflow_response"]["reused_submission"])
        trigger.assert_called_once()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(TrainingRun)), 1)

    def test_same_request_id_with_changed_window_is_rejected(self):
        with patch.object(datasets, "get_settings", return_value=self.settings), \
             patch.object(datasets.airflow_client, "trigger_dag", return_value={}) as trigger:
            self.assertEqual(self.client.post(self.url, json=self.payload).status_code, 202)
            changed = self.client.post(self.url, json={**self.payload, "train_end_date": "2026-10-01"})
        self.assertEqual(changed.status_code, 409)
        trigger.assert_called_once()

    def publish_neural_configuration(self):
        # Real result publication stores midnight datetime strings in the
        # run's metadata; the browser retries its original date-only payload.
        run = self.db.scalar(select(TrainingRun))
        run.status = TrainingRunStatus.COMPLETED
        run.configuration_json = {**run.configuration_json,
            'model_family': 'neural_sequence',
            'train_start_date': '2026-01-01T00:00:00',
            'train_end_date': '2026-11-01T00:00:00'}
        self.db.commit()
        return str(run.id)

    def test_completed_neural_request_reuses_original_run_without_dispatch(self):
        with patch.object(datasets, 'get_settings', return_value=self.settings), \
             patch.object(datasets.airflow_client, 'trigger_dag', return_value={}) as trigger:
            self.assertEqual(self.client.post(self.url, json=self.payload).status_code, 202)
            original_id = self.publish_neural_configuration()
            response = self.client.post(self.url, json=self.payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['training_run']['id'], original_id)
        self.assertTrue(response.json()['airflow_response']['reused_submission'])
        trigger.assert_called_once()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(TrainingRun)), 1)
        saved = self.db.get(TrainingRun, uuid.UUID(original_id))
        self.assertEqual(saved.configuration_json['train_start_date'], '2026-01-01T00:00:00')
        self.assertEqual(saved.configuration_json['train_end_date'], '2026-11-01T00:00:00')

    def test_completed_request_still_rejects_changed_training_parameters(self):
        for change in ({'train_end_date': '2026-10-01'}, {'selected_models': ['lstm']}):
            with self.subTest(change=change), \
                 patch.object(datasets, 'get_settings', return_value=self.settings), \
                 patch.object(datasets.airflow_client, 'trigger_dag', return_value={}) as trigger:
                # Each request UUID identifies its own original submission.
                self.payload['request_id'] = str(uuid.uuid4())
                self.assertEqual(self.client.post(self.url, json=self.payload).status_code, 202)
                run = self.db.scalar(select(TrainingRun).where(
                    TrainingRun.configuration_json['request_id'].as_string() == self.payload['request_id']))
                run.status = TrainingRunStatus.COMPLETED
                run.configuration_json = {**run.configuration_json,
                    'train_start_date': '2026-01-01T00:00:00', 'train_end_date': '2026-11-01T00:00:00'}
                self.db.commit()
                response = self.client.post(self.url, json={**self.payload, **change})
                self.assertEqual(response.status_code, 409)
                trigger.assert_called_once()

    def test_non_midnight_saved_window_is_not_silently_truncated(self):
        with patch.object(datasets, 'get_settings', return_value=self.settings), \
             patch.object(datasets.airflow_client, 'trigger_dag', return_value={}) as trigger:
            self.assertEqual(self.client.post(self.url, json=self.payload).status_code, 202)
            self.publish_neural_configuration()
            run = self.db.scalar(select(TrainingRun))
            run.configuration_json = {**run.configuration_json, 'train_end_date': '2026-11-01T12:00:00'}
            self.db.commit()
            response = self.client.post(self.url, json=self.payload)
        self.assertEqual(response.status_code, 409)
        trigger.assert_called_once()

    def test_new_request_still_requires_date_only_input(self):
        with patch.object(datasets, 'get_settings', return_value=self.settings), \
             patch.object(datasets.airflow_client, 'trigger_dag') as trigger:
            response = self.client.post(self.url, json={**self.payload, 'train_start_date': '2026-01-01T00:00:00'})
        self.assertEqual(response.status_code, 422)
        trigger.assert_not_called()

    def test_local_retrain_is_rejected_before_any_run_or_dispatch(self):
        with patch.object(datasets, "get_settings", return_value=self.settings), \
             patch.object(datasets.airflow_client, "trigger_dag") as trigger:
            response = self.client.post(self.url, json={**self.payload, "training_backend": "local", "trigger_source": "feature_drift"})
        self.assertEqual(response.status_code, 422)
        trigger.assert_not_called()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(TrainingRun)), 0)


if __name__ == "__main__":
    unittest.main()
