"""Durable-state tests; no fitting, external services or real database writes."""
import copy
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.core.errors import ApplicationError
from backend.app.db.models import Base, Dataset, DatasetStatus, ModelVersion, Region, TrainingRun, TrainingRunStatus, User, UserRole
from backend.app.db.training_execution import TrainingExecution
from backend.app.services import training_execution as service
from backend.app.services.model_registry import create_queued_training_run, register_training_result
from backend.app.schemas.model_version import TrainingRunCreate


class TrainingExecutionTests(unittest.TestCase):
    def test_publication_timing_is_recorded_after_commit_without_republishing(self):
        self.assertFalse(service.record_finalization_timing(self.db, self.run.id, 3))
        for branch in service.BRANCHES:
            attempt = self.claim(branch)
            service.branch_ready(self.db, self.run.id, branch, attempt, self.receipt(branch))
        record = service.publication_gate(self.db, self.run.id)
        service.publication_gate(self.db, self.run.id)
        self.assertEqual(sum(event["code"] == "publication_started" for event in record.events_json), 1)
        service.complete_in_transaction(record)
        self.db.commit()
        self.assertTrue(service.record_finalization_timing(self.db, self.run.id, 3))
        self.assertFalse(service.record_finalization_timing(self.db, self.run.id, 4))
        record = service.execution(self.db, self.run.id)
        self.assertEqual(record.branches_json["tree"]["finalization"]["seconds"], 3)
        self.assertEqual(record.state, "completed")

    def timed(self, sequence, stage, elapsed, **details):
        return self.event(sequence, model="lstm", stage=stage, source_clock="ml_child_monotonic_v1",
                          source_time="2026-11-09T00:00:00+00:00", source_elapsed_seconds=elapsed, **details)

    def test_delayed_burst_preserves_source_duration_and_cv_is_not_final_completion(self):
        attempt = self.claim("lstm")
        received = datetime(2026, 11, 9, 0, 20, tzinfo=UTC)
        with patch.object(service, "now", return_value=received):
            service.progress(self.db, self.run.id, "lstm", attempt, self.timed(1, "fit_started", 10, fit_id=1, fit_purpose="cv_full", fold=1, folds=3))
            service.progress(self.db, self.run.id, "lstm", attempt, self.timed(2, "fit_completed", 40, fit_id=1, fit_purpose="cv_full", epochs=10, epochs_completed=3, early_stopped=True))
            service.progress(self.db, self.run.id, "lstm", attempt, self.timed(3, "model_cv_completed", 41))
            record = service.execution(self.db, self.run.id)
            view = service.snapshot(record, self.run, self.db.get(Dataset, self.run.dataset_id))
        self.assertEqual(record.models_json["lstm"]["fits"][0]["seconds"], 30)
        self.assertTrue(record.models_json["lstm"]["fits"][0]["early_stopped"])
        self.assertEqual(view["evaluated_models"], 0)
        self.assertEqual(view["cv_completed_models"], 1)
        self.assertEqual(view["output_verified_models"], 0)
        self.assertEqual({event["time"] for event in record.events_json[-3:]}, {received.isoformat()})
        service.progress(self.db, self.run.id, "lstm", attempt, self.timed(4, "final_fit_started", 50))
        service.progress(self.db, self.run.id, "lstm", attempt, self.timed(5, "fit_started", 51, fit_id=2, fit_purpose="final"))
        service.progress(self.db, self.run.id, "lstm", attempt, self.timed(6, "model_evaluated", 70))
        self.assertEqual(service.execution(self.db, self.run.id).models_json["lstm"]["phase_seconds"]["waiting_final"], 9)

    def test_heartbeat_does_not_make_old_ml_progress_look_fresh(self):
        attempt = self.claim("lstm")
        start = datetime(2026, 11, 9, tzinfo=UTC)
        with patch.object(service, "now", return_value=start):
            service.progress(self.db, self.run.id, "lstm", attempt, self.event(model="lstm"))
        with patch.object(service, "now", return_value=start + timedelta(minutes=5)):
            service.heartbeat(self.db, self.run.id, "lstm", attempt)
            record = service.execution(self.db, self.run.id)
            view = service.snapshot(record, self.run, self.db.get(Dataset, self.run.dataset_id))
        model = next(model for model in view["models"] if model["id"] == "lstm")
        self.assertEqual(model["ml_age_seconds"], 300)
        self.assertNotEqual(model["last_ml_received_at"], view["branches"]["lstm"]["heartbeat_at"])

    def test_legacy_cv_flag_is_not_counted_as_finished_ml_and_unknown_timing_stays_unknown(self):
        record = service.execution(self.db, self.run.id)
        record.models_json = {"lstm": {"name": "LSTM", "state": "evaluated", "stage": "model_cv_completed", "evaluation_completed": True}}
        view = service.snapshot(record, self.run, self.db.get(Dataset, self.run.dataset_id))
        self.assertEqual(view["evaluated_models"], 0)
        self.assertEqual(view["cv_completed_models"], 1)
        self.assertEqual(view["models"][0]["phase"], "waiting_final")
        self.assertNotIn("source_time", view["models"][0])

    def test_timing_and_history_survive_provider_cleanup_without_exposing_snapshot(self):
        attempt = self.claim("lstm")
        provider = {"backend": "kaggle", "snapshot": {"secret": "DO_NOT_EXPOSE"}, "reference": "PRIVATE_REFERENCE",
                    "phase": "branch_ready", "telemetry": {"version": 1, "operations": {"status": {"seconds": 3, "count": 2}}}}
        service.update_provider(self.db, self.run.id, "lstm", attempt, {**provider, "notebook_cleaned": True, "dataset_cleaned": True})
        record = service.execution(self.db, self.run.id)
        view = service.snapshot(record, self.run, self.db.get(Dataset, self.run.dataset_id))
        self.assertEqual(view["branches"]["lstm"]["observation"]["telemetry"], provider["telemetry"])
        self.assertNotIn("DO_NOT_EXPOSE", str(view))
        self.assertNotIn("PRIVATE_REFERENCE", str(view))

    def test_source_clock_regression_invalid_fit_and_partial_timing_are_rejected(self):
        attempt = self.claim("lstm")
        service.progress(self.db, self.run.id, "lstm", attempt, self.timed(1, "model_started", 10))
        invalid = [self.timed(2, "epoch_started", 9), self.event(2, model="lstm", source_time="not-a-clock"),
                   self.timed(2, "fit_started", 11, fit_id=1),
                   self.timed(2, "fit_completed", 12, epochs=3, epochs_completed=4),
                   self.timed(2, "fit_completed", 12, early_stopped="true")]
        for event in invalid:
            with self.subTest(event=event), self.assertRaises(ApplicationError):
                service.progress(self.db, self.run.id, "lstm", attempt, event)

    def test_per_model_history_is_bounded_and_duplicate_event_does_not_advance_it(self):
        attempt = self.claim("lstm")
        for sequence in range(1, 62):
            service.progress(self.db, self.run.id, "lstm", attempt, self.timed(sequence, "epoch_completed", sequence, epoch=sequence, epochs=100))
        record = service.execution(self.db, self.run.id)
        self.assertEqual(len(record.models_json["lstm"]["history"]), 48)
        count = len(record.events_json)
        self.assertFalse(service.progress(self.db, self.run.id, "lstm", attempt, self.timed(60, "epoch_completed", 60)))
        self.assertEqual(len(service.execution(self.db, self.run.id).events_json), count)

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

    def failed_dispatch(self):
        record = service.execution(self.db, self.run.id)
        record.created_at = datetime.now(UTC) - timedelta(minutes=1)
        self.db.commit()
        from backend.app.core.config import get_settings
        return {'dag_id': get_settings().training_dag_id, 'dag_run_id': self.run.airflow_dag_run_id,
                'state': 'failed', 'conf': {'training_run_id': str(self.run.id),
                    'region_id': str(self.run.region_id), 'dataset_id': str(self.run.dataset_id)}}

    def test_airflow_terminal_failure_closes_unstarted_dispatch_and_monitor_is_read_only(self):
        document = self.failed_dispatch()
        record = service.execution(self.db, self.run.id)
        configuration = copy.deepcopy(self.run.configuration_json)
        self.assertEqual(service.visible_runs(self.db, self.region.id)[0]['state'], 'queued')
        self.assertEqual(record.state, 'queued')
        self.assertTrue(service.reconcile_dispatch(self.db, self.run.id, fetch=lambda *args: document))
        self.assertEqual(record.state, 'failed')
        self.assertEqual(self.run.status, TrainingRunStatus.FAILED)
        self.assertEqual(service.visible_runs(self.db, self.region.id)[0]['error_code'], 'dispatch_failed')
        self.assertTrue(all(b['cleanup_confirmed'] for b in record.branches_json.values()))
        self.assertEqual(self.run.configuration_json, configuration)
        with self.assertRaises(ApplicationError): self.claim()

    def test_unavailable_nonterminal_wrong_identity_or_conf_never_closes_dispatch(self):
        document = self.failed_dispatch()
        cases = [None, {**document, 'state': 'queued'}, {**document, 'state': 'running'},
                 {**document, 'state': 'success'}, {**document, 'dag_run_id': 'another-run'},
                 {**document, 'dag_id': 'another-dag'}, {**document, 'conf': {}},
                 {**document, 'conf': {**document['conf'], 'training_run_id': str(uuid.uuid4())}}]
        for item in cases:
            with self.subTest(item=item):
                def fetch(*args):
                    if item is None: raise TimeoutError('PRIVATE HTTP RESPONSE')
                    return item
                self.assertFalse(service.reconcile_dispatch(self.db, self.run.id, fetch=fetch))
                self.assertEqual(service.execution(self.db, self.run.id).state, 'queued')

    def test_recent_queue_or_already_claimed_branch_is_not_polled_or_closed(self):
        from unittest.mock import Mock
        fetch = Mock()
        self.assertFalse(service.reconcile_dispatch(self.db, self.run.id, fetch=fetch))
        self.failed_dispatch()
        self.claim()
        self.assertFalse(service.reconcile_dispatch(self.db, self.run.id, fetch=fetch))
        fetch.assert_not_called()

    def test_worker_claimed_during_airflow_read_keeps_its_owner_and_open_run(self):
        document = self.failed_dispatch()
        attempts = []
        def fetch(*args):
            attempts.append(self.claim())
            return document
        self.assertFalse(service.reconcile_dispatch(self.db, self.run.id, fetch=fetch))
        record = service.execution(self.db, self.run.id)
        self.assertEqual(record.branches_json['tree']['attempt_id'], attempts[0])
        self.assertEqual(record.state, 'running')
        self.assertFalse(record.cancel_requested)

    def test_dispatch_binding_changed_during_read_cannot_close_another_dag_run(self):
        document = self.failed_dispatch()
        def fetch(*args):
            self.run.airflow_dag_run_id = 'changed-before-lock'
            self.db.commit()
            return document
        self.assertFalse(service.reconcile_dispatch(self.db, self.run.id, fetch=fetch))
        self.assertEqual(service.execution(self.db, self.run.id).state, 'queued')

    def test_backend_is_manual_and_validated(self):
        self.assertEqual(TrainingRunCreate(train_start_date="2026-01-01", train_end_date="2026-02-01").training_backend, "kaggle")
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
        self.assertTrue(model["cv_completed"])
        self.assertFalse(model["evaluation_completed"])
        self.assertFalse(model["ml_completed"])
        self.assertEqual(model["phase"], "final_fit")

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
