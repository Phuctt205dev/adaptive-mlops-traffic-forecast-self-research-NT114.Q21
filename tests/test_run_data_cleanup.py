"""Synthetic SQLite/files/S3 only: never delete production data for a test."""
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as Obj
from unittest.mock import Mock, patch
import unittest
import uuid

from fastapi import FastAPI
from sqlalchemy import select

from backend.app.core.errors import ApplicationError
from backend.app.db.models import AuditLog, ModelVersion, TrainingRun, TrainingRunStatus
from backend.app.db.training_execution import TrainingExecution
from backend.app.services import run_data_cleanup as cleanup
from backend.app.services.region_cleanup import MODEL_COMPLETED, MODEL_PENDING, pending_cleanup
from backend.app.services.region_cleanup_runtime import purge_local
from tests import test_region_cleanup as fixtures
from tests.test_region_cleanup import FakeResources


class RunDataCleanupTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    tearDown = fixtures.CleanupTests.tearDown
    make_region = fixtures.CleanupTests.make_region

    def orphan(self, region=None, status=TrainingRunStatus.COMPLETED):
        run = TrainingRun(region_id=(region or self.region).id, dataset_id=self.dataset.id, status=status,
                          split_policy="test", requested_by=self.user.id)
        self.db.add(run)
        self.db.commit()
        return run

    def test_a_run_without_a_model_is_cleaned_and_its_record_and_other_runs_stay(self):
        run = self.orphan()
        run_id, other_run = run.id, self.run.id
        backend = FakeResources()
        cleanup.delete_run_data(self.db, run_id, backend=backend)
        self.assertEqual(backend.calls, ["local_files", "mlflow", "objects", "verify"])
        self.assertIsNotNone(self.db.get(TrainingRun, run_id))
        self.assertIsNotNone(self.db.get(TrainingRun, other_run))
        self.assertIsNotNone(self.db.get(ModelVersion, self.model.id))
        journal = self.db.scalar(select(AuditLog).where(AuditLog.entity_id == run_id))
        self.assertEqual((journal.action, journal.after_json["state"]), (MODEL_COMPLETED, "completed"))
        self.assertEqual(journal.after_json["plan"]["kind"], "training_run_without_model")
        self.assertIsNone(pending_cleanup(self.db))
        cleanup.delete_run_data(self.db, run_id, backend=backend)  # A repeated request is a no-op.
        self.assertEqual(backend.preflights, 1)

    def test_runs_that_may_still_be_needed_are_refused_before_anything_is_touched(self):
        running = self.orphan(status=TrainingRunStatus.TRAINING)
        busy = self.orphan()
        self.db.add(TrainingExecution(training_run_id=busy.id, backend="kaggle", state="running"))
        mentioned = self.orphan()
        self.model.artifact_uri = f"models/distributed-serving/v1/{self.region.id}/{mentioned.id}/bundle/model.pkl"
        self.db.commit()
        cases = {self.run.id: "a registered model still uses this run",
                 running.id: "the run is still training",
                 busy.id: "the execution is not finished",
                 mentioned.id: "a live model's files or configuration point at this run"}
        for run_id, reason in cases.items():
            backend = FakeResources()
            with self.subTest(reason=reason), self.assertRaises(ApplicationError) as caught:
                cleanup.delete_run_data(self.db, run_id, backend=backend)
            self.assertEqual(caught.exception.code, "run_cleanup_conflict")
            self.assertIn(reason, caught.exception.message)
            self.assertEqual((backend.calls, backend.preflights), ([], 0))
            self.assertIsNone(pending_cleanup(self.db))

    def test_unknown_run_and_unfinished_other_deletion_are_refused(self):
        with self.assertRaises(ApplicationError) as caught:
            cleanup.delete_run_data(self.db, uuid.uuid4(), backend=FakeResources())
        self.assertEqual(caught.exception.status_code, 404)
        self.db.add(AuditLog(action=MODEL_PENDING, entity_type="model", entity_id=uuid.uuid4(),
                             after_json={"state": "pending", "plan": {}, "done": []}))
        self.db.commit()
        with self.assertRaises(ApplicationError) as caught:
            cleanup.delete_run_data(self.db, self.orphan().id, backend=FakeResources())
        self.assertEqual(caught.exception.code, "run_cleanup_pending")

    def test_failed_step_keeps_the_inventory_and_retry_resumes_without_a_new_preflight(self):
        run_id = self.orphan().id
        backend = FakeResources("objects")
        with self.assertRaises(ApplicationError) as caught:
            cleanup.delete_run_data(self.db, run_id, backend=backend)
        self.assertEqual(caught.exception.code, "run_cleanup_incomplete")
        journal = pending_cleanup(self.db)
        self.assertEqual((journal.action, journal.after_json["state"]), (MODEL_PENDING, "retry_required"))
        self.assertEqual(journal.after_json["done"], ["local_files", "mlflow"])
        backend.failure, backend.calls = None, []
        cleanup.delete_run_data(self.db, run_id, backend=backend)
        self.assertEqual(backend.calls, ["local_files", "objects", "verify"])
        self.assertEqual(backend.preflights, 1)
        self.assertIsNone(pending_cleanup(self.db))

    def test_peers_purge_only_this_runs_local_directories(self):
        run = self.orphan()
        plan = cleanup.inventory(self.db, run)
        with TemporaryDirectory() as directory:
            owned = [Path(directory) / prefix for prefix in plan["local_prefixes"]]
            protected = Path(directory) / f"models/distributed-serving/v1/{self.region.id}/{self.run.id}/bundle"
            for folder in (*owned, protected):
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "model.pkl").write_bytes(b"synthetic")
            purge_local(FastAPI(), plan, root=directory)
            self.assertFalse(any(folder.exists() for folder in owned))
            self.assertTrue((protected / "model.pkl").exists())

    def resource(self):
        resource = object.__new__(cleanup.RunDataCleanupResources)
        resource.runtime_bucket, resource.mlflow_bucket = "runtime", "tracking"
        resource.check_idle, resource.objects = Mock(), Mock(return_value=[])
        resource.multipart_uploads = Mock(return_value=[])
        return resource

    def tracking(self, run, **tags):
        identity = uuid.uuid4().hex
        return Obj(info=Obj(run_id=identity, experiment_id="5", status="FINISHED", lifecycle_stage="active",
                            artifact_uri=f"mlflow-artifacts:/5/{identity}/artifacts"),
                   data=Obj(tags={"traffic_training_run_id": str(run.id), "traffic_region_id": str(run.region_id), **tags}))

    def preflight(self, entries, run):
        client = Mock()
        client.search_experiments.return_value = [Obj(experiment_id="5")]
        client.search_runs.side_effect = lambda experiments, filter_string, run_view_type, max_results: (
            entries if "traffic_training_run_id" in filter_string else [])
        with patch.dict("sys.modules", {"mlflow.tracking": Obj(MlflowClient=Mock(return_value=client))}):
            return self.resource().preflight(self.db, cleanup.inventory(self.db, run))

    def test_real_preflight_targets_only_this_runs_files_and_tracking_runs(self):
        run = self.orphan()
        entry = self.tracking(run)
        plan = self.preflight([entry], run)
        prefixes = {(item["bucket"], item["prefix"]) for item in plan["objects"]}
        self.assertIn(("runtime", f"models/distributed-serving/v1/{run.region_id}/{run.id}/"), prefixes)
        self.assertIn(("runtime", f"training-exchange/v1/regions/{run.region_id}/runs/{run.id}/"), prefixes)
        self.assertIn(("runtime", f"local-training/v1/{run.id}/"), prefixes)
        self.assertIn(("tracking", f"5/{entry.info.run_id}/"), prefixes)
        self.assertFalse(any(str(self.run.id) in prefix for _, prefix in prefixes))
        self.assertFalse(any(prefix in {"", "5/"} or prefix.endswith("regions/") for _, prefix in prefixes))
        self.assertEqual(plan["mlflow_runs"], [entry.info.run_id])

    def test_tracking_runs_of_another_region_or_used_by_a_model_are_rejected(self):
        run = self.orphan()
        for label, entry in (
            ("other region", self.tracking(run, traffic_region_id=str(self.other.id))),
            ("used by a model", self.tracking(run)),
            ("still running", self.tracking(run)),
        ):
            if label == "used by a model":
                self.model.mlflow_run_id = entry.info.run_id
                self.db.commit()
            if label == "still running":
                entry.info.status = "RUNNING"
            with self.subTest(label), self.assertRaises(ApplicationError):
                self.preflight([entry], run)


if __name__ == "__main__":
    unittest.main()
