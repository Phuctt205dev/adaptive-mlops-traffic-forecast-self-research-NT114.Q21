"""Durable-state tests; no fitting, external services or real database writes."""
import copy
import unittest
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.core.errors import ApplicationError
from backend.app.db.models import Base, Dataset, DatasetStatus, ModelVersion, Region, TrainingRun, TrainingRunStatus, User, UserRole
from backend.app.db.training_execution import TrainingExecution
from backend.app.services import training_execution as service
from backend.app.services.model_registry import create_queued_training_run, register_training_result
from backend.app.schemas.model_version import TrainingRunCreate


class TrainingExecutionTests(unittest.TestCase):
    def test_secondary_stop_callback_keeps_first_failure_and_does_not_duplicate_logs(self):
        tree = self.claim("tree")
        neural = self.claim("lstm")
        self.assertTrue(service.fail_run(self.db, self.run.id, "lstm", neural, "cloud_unavailable"))
        sequence = service.execution(self.db, self.run.id).sequence
        self.assertFalse(service.fail_run(self.db, self.run.id, "tree", tree, "monitoring_timeout"))
        record = service.execution(self.db, self.run.id)
        self.assertEqual(record.error_code, "cloud_unavailable")
        self.assertEqual(record.sequence, sequence)
        self.assertNotEqual(record.branches_json["tree"]["state"], "failed")
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        user = User(email="probe@example.com", password_hash="hash", role=UserRole.ADMIN)
        region = Region(name="Probe", latitude="10", longitude="106", timezone="Asia/Ho_Chi_Minh")
        self.db.add_all([user, region])
        self.db.flush()
        dataset = Dataset(region_id=region.id, contract_version="traffic-training-csv/v1", original_filename="probe.csv",
                          storage_uri="s3://probe/input.csv", sha256="a" * 64, row_count=5000,
                          status=DatasetStatus.VALID, uploaded_by=user.id)
        self.db.add(dataset)
        self.db.flush()
        self.config = {"training_backend": "kaggle", "selected_models": ["random_forest_lag", "lstm", "gru"],
                       "_execution_protocol": 2, "cv_splits": 3}
        self.run = create_queued_training_run(self.db, dataset.id, user.id, copy.deepcopy(self.config), "probe-" + str(uuid.uuid4()))
        self.region = region

    def claim(self, branch="tree"):
        return service.claim_branch(self.db, self.run.id, branch, "probe-worker")["attempt_id"]

    def receipt(self, branch):
        return {"version": 1, "run_id": str(self.run.id), "region_id": str(self.run.region_id),
                "branch": branch, "bundle_id": str(uuid.uuid4()), "manifest_sha256": "b" * 64}

    def event(self, seq=1, model="random_forest_lag", stage="model_started", **details):
        return {"sequence": seq, "model": model, "stage": stage, **details}

    def test_backend_is_manual_and_validated(self):
        self.assertEqual(TrainingRunCreate(train_start_date="2026-01-01", train_end_date="2026-02-01").training_backend, "local")
        with self.assertRaises(ValueError):
            TrainingRunCreate(train_start_date="a", train_end_date="b", training_backend="automatic")

    def test_claim_is_idempotent_and_cannot_change_backend(self):
        first = self.claim()
        self.assertEqual(self.claim(), first)
        self.assertEqual(service.execution(self.db, self.run.id).backend, "kaggle")
        self.assertEqual(self.db.get(TrainingRun, self.run.id).configuration_json, self.config)
        with self.assertRaises(ApplicationError):
            service.claim_branch(self.db, self.run.id, "tree", "restarted-worker")

    def test_dispatch_failure_closes_queued_run_but_requires_started_child_ack(self):
        service.fail_dispatch(self.db, self.run.id)
        self.assertEqual(service.execution(self.db, self.run.id).state, "failed")
        with self.assertRaises(ApplicationError):
            self.claim()

    def test_dispatch_response_lost_after_start_requires_real_stop(self):
        attempt = self.claim()
        service.fail_dispatch(self.db, self.run.id)
        self.assertEqual(service.execution(self.db, self.run.id).state, "cancelling")
        service.acknowledge_stop(self.db, self.run.id, "tree", attempt, stopped=True, cleaned=True)
        self.assertEqual(service.execution(self.db, self.run.id).state, "failed")

    def test_duplicate_progress_and_late_attempt_are_rejected(self):
        attempt = self.claim()
        self.assertTrue(service.progress(self.db, self.run.id, "tree", attempt, self.event()))
        self.assertFalse(service.progress(self.db, self.run.id, "tree", attempt, self.event()))
        with self.assertRaises(ApplicationError):
            service.progress(self.db, self.run.id, "tree", str(uuid.uuid4()), self.event(2))

    def test_progress_cannot_inject_secret_fields_or_nonfinite_loss(self):
        attempt = self.claim()
        for event in (self.event(token="never-store"), self.event(loss=float("nan")), self.event(fold=True)):
            with self.subTest(event=event), self.assertRaises(ApplicationError):
                service.progress(self.db, self.run.id, "tree", attempt, event)
        self.assertNotIn("never-store", str(service.execution(self.db, self.run.id).events_json))

    def test_model_and_branch_must_match(self):
        attempt = self.claim()
        with self.assertRaises(ApplicationError):
            service.progress(self.db, self.run.id, "tree", attempt, self.event(model="lstm"))

    def test_failure_cancels_run_and_blocks_publication_without_fallback(self):
        tree = self.claim()
        neural = self.claim("lstm")
        service.progress(self.db, self.run.id, "tree", tree, self.event())
        service.progress(self.db, self.run.id, "lstm", neural, self.event(model="lstm"))
        service.fail_run(self.db, self.run.id, "lstm", neural, "cloud_unavailable")
        record = service.execution(self.db, self.run.id)
        self.assertEqual(record.state, "cancelling")
        self.assertTrue(record.cancel_requested)
        self.assertEqual(record.backend, "kaggle")
        self.assertEqual(record.models_json["gru"]["state"], "cancelled")
        for operation in (lambda: self.claim("gru"), lambda: service.publication_gate(self.db, self.run.id),
                          lambda: service.branch_ready(self.db, self.run.id, "tree", tree, self.receipt("tree"))):
            with self.assertRaises(ApplicationError):
                operation()

    def test_failed_run_cannot_register_a_model_or_change_the_active_model(self):
        attempt = self.claim()
        service.fail_run(self.db, self.run.id, "tree", attempt)
        with self.assertRaises(ApplicationError):
            register_training_result(self.db, self.run.region_id, self.run.dataset_id, {}, self.run.id)
        self.assertEqual(self.db.query(ModelVersion).count(), 0)
        self.assertIsNone(self.db.get(Region, self.run.region_id).active_model_version_id)

    def test_stop_is_not_confirmed_until_all_started_branches_acknowledge(self):
        tree, neural = self.claim(), self.claim("lstm")
        service.fail_run(self.db, self.run.id, "lstm", neural)
        service.acknowledge_stop(self.db, self.run.id, "lstm", neural, stopped=True, cleaned=True)
        self.assertEqual(service.execution(self.db, self.run.id).state, "cancelling")
        service.acknowledge_stop(self.db, self.run.id, "tree", tree, stopped=False, cleaned=True)
        self.assertEqual(service.execution(self.db, self.run.id).state, "cancelling")
        with self.assertRaises(ApplicationError):
            service.dismiss_error(self.db, self.run.id)
        service.acknowledge_stop(self.db, self.run.id, "tree", tree, stopped=True, cleaned=True)
        self.assertEqual(service.execution(self.db, self.run.id).state, "failed")
        self.assertEqual(service.execution(self.db, self.run.id).models_json["random_forest_lag"]["state"], "cancelled")
        self.assertEqual(self.db.get(TrainingRun, self.run.id).status, TrainingRunStatus.FAILED)
        self.assertFalse(service.fail_run(self.db, self.run.id, "tree", tree))
        service.acknowledge_stop(self.db, self.run.id, "tree", tree, stopped=False, cleaned=False)
        self.assertEqual(service.execution(self.db, self.run.id).state, "failed")
        self.assertTrue(service.execution(self.db, self.run.id).branches_json["tree"]["cleanup_confirmed"])

    def test_stop_confirmation_does_not_accept_truthy_strings(self):
        attempt = self.claim()
        service.fail_run(self.db, self.run.id, "tree", attempt)
        with self.assertRaises(ApplicationError):
            service.acknowledge_stop(self.db, self.run.id, "tree", attempt, stopped="false", cleaned=True)

    def test_final_fit_clears_fold_counters_without_losing_cv_progress(self):
        attempt = self.claim()
        service.progress(self.db, self.run.id, "tree", attempt, self.event(fold=3, folds=3))
        service.progress(self.db, self.run.id, "tree", attempt, self.event(2, stage="model_cv_completed"))
        service.progress(self.db, self.run.id, "tree", attempt, self.event(3, stage="final_fit_started"))
        model = service.execution(self.db, self.run.id).models_json["random_forest_lag"]
        self.assertNotIn("fold", model)
        self.assertTrue(model["evaluation_completed"])

    def test_receipts_required_and_cannot_change_after_acceptance(self):
        attempt = self.claim()
        with self.assertRaises(ApplicationError):
            service.branch_ready(self.db, self.run.id, "tree", attempt, self.receipt("lstm"))
        with self.assertRaises(ApplicationError):
            service.branch_ready(self.db, self.run.id, "tree", attempt, self.receipt("tree"), skipped=True)
        receipt = self.receipt("tree")
        service.branch_ready(self.db, self.run.id, "tree", attempt, receipt)
        service.branch_ready(self.db, self.run.id, "tree", attempt, receipt)
        with self.assertRaises(ApplicationError):
            service.branch_ready(self.db, self.run.id, "tree", attempt, self.receipt("tree"))

    def test_success_is_hidden_only_after_final_publication(self):
        for branch in service.BRANCHES:
            attempt = self.claim(branch)
            service.branch_ready(self.db, self.run.id, branch, attempt, self.receipt(branch))
        self.assertEqual(len(service.visible_runs(self.db, self.region.id)), 1)
        record = service.publication_gate(self.db, self.run.id)
        self.assertEqual(record.state, "finalizing")
        self.assertEqual(len(service.visible_runs(self.db, self.region.id)), 1)
        service.complete_in_transaction(record)
        self.db.commit()
        self.assertEqual(service.visible_runs(self.db, self.region.id), [])

    def test_error_remains_visible_until_stopped_and_dismissed(self):
        attempt = self.claim()
        service.fail_run(self.db, self.run.id, "tree", attempt)
        service.acknowledge_stop(self.db, self.run.id, "tree", attempt, stopped=True, cleaned=True)
        self.assertEqual(len(service.visible_runs(self.db, self.region.id)), 1)
        service.dismiss_error(self.db, self.run.id)
        self.assertEqual(service.visible_runs(self.db, self.region.id), [])


if __name__ == "__main__":
    unittest.main()
