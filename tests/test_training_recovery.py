"""Cancellation, retry lineage and uncertain-provider fencing against an isolated DB."""
import copy
import unittest
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from backend.app.core.errors import ApplicationError
from backend.app.db.models import Base, Dataset, DatasetStatus, Region, TrainingRun, User, UserRole
from backend.app.schemas.model_version import TrainingRunCreate
from backend.app.services.model_registry import create_queued_training_run
from backend.app.services import training_execution as state


class TrainingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        admin = User(email="training@example.test", password_hash="not-a-password", role=UserRole.ADMIN)
        region = Region(name="Synthetic", latitude="10", longitude="106", timezone="UTC")
        self.db.add_all([admin, region]); self.db.flush()
        dataset = Dataset(region_id=region.id, contract_version="traffic-training-csv/v1", original_filename="synthetic.csv",
                          storage_uri="s3://synthetic/input", sha256="a" * 64, row_count=5000,
                          status=DatasetStatus.VALID, uploaded_by=admin.id)
        self.db.add(dataset); self.db.flush()
        self.config = {**TrainingRunCreate(train_start_date="2026-01-01", train_end_date="2026-11-01").model_dump(mode="json"), "_execution_protocol": 2}
        self.run = create_queued_training_run(self.db, dataset.id, admin.id, copy.deepcopy(self.config), "synthetic")

    def test_cancel_queued_run_is_terminal_without_creating_an_attempt(self):
        record = state.request_cancel(self.db, self.run.id)
        self.assertEqual(record.state, "cancelled")
        self.assertTrue(record.cancel_requested)
        self.assertTrue(all(v.get("stop_confirmed") and v.get("cleanup_confirmed") for v in record.branches_json.values()))
        with self.assertRaises(ApplicationError):
            state.claim_branch(self.db, self.run.id, "tree", "worker")

    def test_running_cancel_waits_for_real_stop_and_cleanup_and_is_not_failed(self):
        attempts = {branch: state.claim_branch(self.db, self.run.id, branch, "worker")["attempt_id"] for branch in state.BRANCHES}
        state.request_cancel(self.db, self.run.id)
        state.acknowledge_stop(self.db, self.run.id, "tree", attempts["tree"], stopped=True, cleaned=False)
        self.assertEqual(state.execution(self.db, self.run.id).state, "cancelling")
        for branch, attempt in attempts.items():
            state.acknowledge_stop(self.db, self.run.id, branch, attempt, stopped=True, cleaned=True)
        self.assertEqual(state.execution(self.db, self.run.id).state, "cancelled")
        self.db.refresh(self.run)
        self.assertEqual(self.run.status.value, "cancelled")

    def test_retry_is_new_run_with_preserved_config_and_no_duplicate_active_retry(self):
        state.request_cancel(self.db, self.run.id)
        configuration = {**self.config, "training_backend": "local", "retry_of": str(self.run.id)}
        child = create_queued_training_run(self.db, self.run.dataset_id, self.run.requested_by, configuration, "retry")
        self.assertNotEqual(child.id, self.run.id)
        self.assertEqual(child.configuration_json["selected_models"], self.config["selected_models"])
        self.assertEqual(state.execution(self.db, child.id).backend, "local")
        with self.assertRaises(ApplicationError):
            create_queued_training_run(self.db, self.run.dataset_id, self.run.requested_by, configuration, "duplicate")

    def test_retry_of_unknown_or_still_running_provider_is_rejected(self):
        state.claim_branch(self.db, self.run.id, "gru", "worker")
        with self.assertRaisesRegex(ApplicationError, "stopped"):
            create_queued_training_run(self.db, self.run.dataset_id, self.run.requested_by,
                {**self.config, "training_backend": "local", "retry_of": str(self.run.id)}, "unsafe")

    def test_retry_cannot_change_time_window_or_models(self):
        state.request_cancel(self.db, self.run.id)
        for change in ({"train_start_date": "2026-02-01"}, {"selected_models": ["gru"]}):
            with self.assertRaisesRegex(ApplicationError, "original"):
                create_queued_training_run(self.db, self.run.dataset_id, self.run.requested_by,
                    {**self.config, "retry_of": str(self.run.id), **change}, "changed")

    def test_airflow_timeout_cannot_turn_unknown_provider_into_confirmed_failure(self):
        attempt = state.claim_branch(self.db, self.run.id, "gru", "worker")["attempt_id"]
        state.update_provider(self.db, self.run.id, "gru", attempt, {"backend": "kaggle", "phase": "needs_attention"})
        state.request_stop(self.db, self.run.id)
        record = state.execution(self.db, self.run.id)
        self.assertEqual(record.state, "running")
        self.assertFalse(record.cancel_requested)
        state.request_cancel(self.db, self.run.id)
        self.assertTrue(state.execution(self.db, self.run.id).cancel_requested)


if __name__ == "__main__":
    unittest.main()
