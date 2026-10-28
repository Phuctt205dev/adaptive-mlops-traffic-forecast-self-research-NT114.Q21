from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.models import Base, Dataset, DatasetStatus, Region, TrainingRun, User, UserRole
from backend.app.services import training_execution as state
from backend.app.services.model_registry import create_queued_training_run
from backend.app.services.training_control import WorkerController
from src.cloud_training.artifacts import write_bundle


class FakeStore:
    prefix, bucket = "training-exchange/v1", "synthetic"
    def __init__(self):
        self.client, self.objects = self, {"models/active/preserve.pkl": b"preserve"}
    def list_objects_v2(self, Bucket, Prefix, MaxKeys, **kwargs):
        return {"Contents": [{"Key": key} for key in list(self.objects) if key.startswith(Prefix)][:MaxKeys]}
    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
        return {}


class FakeBridge:
    def __init__(self):
        self.store = FakeStore()
    def publish_branch(self, run, branch, result):
        prefix = f"{self.store.prefix}/regions/{run.region_id}/runs/{run.id}/branches/{branch}/"
        self.store.objects[prefix + "bundle/manifest"] = b"synthetic"
        return {"version": 1, "run_id": str(run.id), "region_id": str(run.region_id), "branch": branch,
                "bundle_id": str(uuid.uuid4()), "manifest_sha256": "a" * 64}


class FakeChild:
    """No ML fit. Tree emits a valid skipped bundle; LSTM simulates failure."""
    def __init__(self, command, context, **kwargs):
        self.context = context
        self.process = SimpleNamespace(pid=12345)
        self.reader_failed = False
        self.code = 0 if context["branch"] == "tree" else 2
        if self.code == 0:
            output = Path(command[command.index("--output") + 1])
            output.mkdir()
            write_bundle({"branch": "tree", "status": "skipped", "candidates": []}, context, output)
    def drain(self):
        return []
    def returncode(self):
        return self.code
    def stop(self):
        return True


class TrainingControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dataset_path = self.root / "source.csv"
        self.dataset_path.write_bytes(b"a,b\n1,2\n")
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.database_lock = threading.RLock()
        @contextmanager
        def sessions():
            with self.database_lock, Session(self.engine, expire_on_commit=False) as db:
                yield db
        self.sessions = sessions
        with self.sessions() as db:
            user = User(email="synthetic@example.com", password_hash="x", role=UserRole.ADMIN)
            region = Region(name="Synthetic", latitude="10", longitude="106", timezone="Asia/Ho_Chi_Minh")
            db.add_all([user, region]); db.flush()
            dataset = Dataset(region_id=region.id, contract_version="traffic-training-csv/v1", original_filename="synthetic.csv",
                storage_uri="s3://synthetic/input.csv", sha256=hashlib.sha256(self.dataset_path.read_bytes()).hexdigest(),
                row_count=5000, status=DatasetStatus.VALID, uploaded_by=user.id)
            db.add(dataset); db.flush()
            run = create_queued_training_run(db, dataset.id, user.id,
                {"training_backend": "local", "_execution_protocol": 2, "selected_models": ["lstm", "gru"]}, "synthetic-dag")
            self.run_id = run.id
        def context(db, run_id):
            run = db.get(TrainingRun, run_id)
            return run, db.get(Dataset, run.dataset_id), None
        self.training = SimpleNamespace(_get_training_context=context,
            _ensure_dataset_downloaded=lambda dataset, run: self.dataset_path,
            get_settings=lambda: SimpleNamespace(cloud_training_enabled=False))
        self.bridge = FakeBridge()
        self.controllers = {}
        for role in ("tree", "recurrent"):
            controller = WorkerController(self.sessions, self.training, self.bridge, role=role,
                identity={"pod": "synthetic-" + role}, root=self.root / role,
                project=Path(__file__).resolve().parents[1])
            self.controllers[role] = controller
        self.child_patch = patch("backend.app.services.training_control.ExecutionProcess", FakeChild)
        self.child_patch.start(); self.addCleanup(self.child_patch.stop)
        self.tracking_patch = patch("backend.app.services.training_tracking.TrainingTracker")
        tracker = self.tracking_patch.start(); tracker.return_value.cleanup.return_value = True
        self.addCleanup(self.tracking_patch.stop)

    def wait(self, controller):
        threads = [handle["thread"] for handle in controller.handles.values() if handle["thread"]]
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_sibling_failure_blocks_publication_and_cleans_only_this_run(self):
        tree, neural = self.controllers["tree"], self.controllers["recurrent"]
        first = tree.start(self.run_id, "tree")
        self.wait(tree)
        duplicate = tree.start(self.run_id, "tree")
        self.assertEqual(first["attempt_id"], duplicate["attempt_id"])
        with self.sessions() as db:
            self.assertEqual(state.execution(db, self.run_id).branches_json["tree"]["state"], "skipped")
        neural.start(self.run_id, "lstm")
        self.wait(neural)
        tree.sweep(); neural.sweep()
        with self.sessions() as db:
            record = state.execution(db, self.run_id)
            self.assertEqual(record.state, "failed")
            self.assertTrue(all(item["cleanup_confirmed"] for item in record.branches_json.values()))
            self.assertEqual(record.models_json["gru"]["state"], "cancelled")
            self.assertEqual(record.backend, "local")
        self.assertEqual(self.bridge.store.objects, {"models/active/preserve.pkl": b"preserve"})
        self.assertTrue(self.dataset_path.is_file())
        self.assertTrue(tree.operation_lock.acquire(blocking=False)); tree.operation_lock.release()
        self.assertTrue(neural.operation_lock.acquire(blocking=False)); neural.operation_lock.release()

    def test_polls_refresh_airflow_lease_and_hide_the_provider_journal(self):
        controller = self.controllers["tree"]
        started = controller.start(self.run_id, "tree")
        self.wait(controller)
        response = controller.status(self.run_id, "tree", started["attempt_id"], 0)
        self.assertNotIn("provider", response["branches"]["tree"])
        with self.sessions() as db:
            self.assertGreater(state.execution(db, self.run_id).branches_json["tree"]["airflow_lease_until"], time.time())

    def test_boot_fences_a_previous_owner_before_background_network_recovery(self):
        controller = self.controllers["tree"]
        with self.sessions() as db:
            state.claim_branch(db, self.run_id, "tree", "previous-owner")
        controller.boot()
        try:
            with self.sessions() as db:
                record = state.execution(db, self.run_id)
                self.assertTrue(record.cancel_requested)
                self.assertEqual(record.error_code, "worker_restarted")
        finally:
            controller.shutdown.set()
            controller.watcher.join(timeout=3)

    def test_unselected_cloud_branch_does_not_upload_or_allocate_gpu(self):
        with self.sessions() as db:
            original = db.get(TrainingRun, self.run_id)
            run = create_queued_training_run(db, original.dataset_id, original.requested_by,
                {"training_backend": "kaggle", "_execution_protocol": 2, "selected_models": ["lstm"]}, "synthetic-cloud-skip")
            run_id = run.id
        controller = self.controllers["tree"]
        with patch.object(self.training, "_ensure_dataset_downloaded", side_effect=AssertionError("No input needed")), \
             patch.object(controller.transport, "call", side_effect=AssertionError("No cloud job needed")):
            controller.start(run_id, "tree")
            self.wait(controller)
        with self.sessions() as db:
            record = state.execution(db, run_id)
            self.assertEqual(record.backend, "kaggle")
            self.assertEqual(record.branches_json["tree"]["state"], "skipped")


if __name__ == "__main__":
    unittest.main()
