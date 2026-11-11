from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.models import Base, Dataset, DatasetStatus, Region, TrainingRun, User, UserRole
from backend.app.services import training_execution as state
from backend.app.services.model_registry import create_queued_training_run
from backend.app.services.training_control import WorkerController, failure_facts
from src.cloud_training.artifacts import write_bundle
from src.cloud_training.kaggle_execution import TrainingCancelled


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


class FakeCloud:
    """Synthetic provider failure; no SDK, process, or actual training."""
    def __init__(self, transport, directory, ledger, save):
        self.ledger = ledger
    def run(self, progress, cancelled):
        raise RuntimeError('Synthetic cloud execution failure')
    def stop(self):
        return True
    def cleanup(self):
        return True
    @contextmanager
    def measured(self, *args, **kwargs):
        yield


class TrainingControlTests(unittest.TestCase):
    def hold_branches(self, controller):
        entered = {name: threading.Event() for name in ("lstm", "gru")}
        gates = {name: threading.Event() for name in entered}
        def execute(run_id, branch, handle):
            entered[branch].set()
            gates[branch].wait(timeout=5)
            handle["stopped"] = True
            controller.release(handle)
        self.addCleanup(lambda: [gate.set() for gate in gates.values()])
        return entered, gates, execute

    def test_two_neural_attempts_share_one_exclusive_run_until_both_stop(self):
        controller = self.controllers["recurrent"]
        controller.status_state = {"busy": False, "operation_count": 0}
        entered, gates, execute = self.hold_branches(controller)
        with self.sessions() as db:
            original = db.get(TrainingRun, self.run_id)
            other = create_queued_training_run(db, original.dataset_id, original.requested_by,
                original.configuration_json, "synthetic-other-run").id
        with patch.object(controller, "execute", side_effect=execute):
            first = controller.start(self.run_id, "lstm")
            second = controller.start(self.run_id, "gru")
            self.assertTrue(all(event.wait(timeout=1) for event in entered.values()))
            self.assertNotEqual(first["attempt_id"], second["attempt_id"])
            self.assertEqual(controller.start(self.run_id, "lstm")["attempt_id"], first["attempt_id"])
            self.assertEqual(controller._lease_count, 2)
            self.assertEqual(controller.status_state["active_cloud_branches"], 2)
            with self.assertRaisesRegex(Exception, "already owns"):
                controller.start(other, "lstm")
            self.assertFalse(controller.operation_lock.acquire(blocking=False))
            gates["lstm"].set()
            controller.handles[(str(self.run_id), "lstm")]["thread"].join(timeout=2)
            self.assertEqual(controller._lease_count, 1)
            self.assertTrue(controller.status_state["busy"])
            self.assertFalse(controller.operation_lock.acquire(blocking=False))
            gates["gru"].set()
            self.wait(controller)
        self.assertEqual(controller._lease_count, 0)
        self.assertFalse(controller.status_state["busy"])
        self.assertEqual(controller.status_state["operation_count"], 2)
        self.assertTrue(controller.operation_lock.acquire(blocking=False))
        controller.operation_lock.release()

    def test_second_branch_claim_failure_cannot_unlock_its_running_sibling(self):
        controller = self.controllers["recurrent"]
        entered, gates, execute = self.hold_branches(controller)
        with patch.object(controller, "execute", side_effect=execute):
            controller.start(self.run_id, "lstm")
            self.assertTrue(entered["lstm"].wait(timeout=1))
            with patch.object(state, "claim_branch", side_effect=RuntimeError("Synthetic DB refusal")):
                with self.assertRaises(RuntimeError):
                    controller.start(self.run_id, "gru")
            self.assertEqual(controller._lease_count, 1)
            self.assertFalse(controller.operation_lock.acquire(blocking=False))
            gates["lstm"].set()
            self.wait(controller)
        self.assertTrue(controller.operation_lock.acquire(blocking=False))
        controller.operation_lock.release()

    def test_lost_start_snapshot_keeps_running_owner_and_retry_returns_same_attempt(self):
        controller = self.controllers["recurrent"]
        entered, gates, execute = self.hold_branches(controller)
        with patch.object(controller, "execute", side_effect=execute):
            with patch.object(controller, "response", side_effect=RuntimeError("Synthetic snapshot failure")):
                with self.assertRaises(RuntimeError):
                    controller.start(self.run_id, "lstm")
            self.assertTrue(entered["lstm"].wait(timeout=1))
            handle = controller.handles[(str(self.run_id), "lstm")]
            self.assertEqual(controller._lease_count, 1)
            self.assertFalse(controller.operation_lock.acquire(blocking=False))
            self.assertEqual(controller.start(self.run_id, "lstm")["attempt_id"], handle["attempt_id"])
            gates["lstm"].set()
            self.wait(controller)

    def test_cpu_verification_is_serial_and_waiting_sibling_can_cancel(self):
        controller = self.controllers["recurrent"]
        entered, gate, cancelled, errors = threading.Event(), threading.Event(), threading.Event(), []
        def verify(*args):
            entered.set()
            gate.wait(timeout=3)
        def waiting():
            try:
                controller.verify_serving(None, {}, {}, {}, cancelled.is_set)
            except Exception as error:
                errors.append(error)
        with patch.object(controller, "_verify_serving", side_effect=verify) as checks:
            first = threading.Thread(target=controller.verify_serving, args=(None, {}, {}, {}, lambda: False))
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            second = threading.Thread(target=waiting)
            second.start()
            cancelled.set()
            second.join(timeout=2)
            gate.set()
            first.join(timeout=2)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TrainingCancelled)
        checks.assert_called_once()

    def test_concurrent_neural_failure_cancels_sibling_with_isolated_sdk_and_cleanup(self):
        controller = self.controllers["recurrent"]
        barrier, transports, observed_cancel = threading.Barrier(2), [], threading.Event()
        def fork():
            transport = SimpleNamespace(close=lambda: None)
            transports.append(transport)
            return transport
        controller.transport = SimpleNamespace(fork=fork)
        class ParallelCloud(FakeCloud):
            def run(provider, progress, cancelled):
                barrier.wait(timeout=3)
                if provider.ledger["snapshot"]["context"]["branch"] == "lstm":
                    raise RuntimeError("Synthetic first-branch failure")
                for _ in range(300):
                    if cancelled():
                        observed_cancel.set()
                        raise TrainingCancelled("Sibling failed")
                    threading.Event().wait(.005)
                raise AssertionError("Sibling did not see the run fence")
        with patch("backend.app.services.training_control.KaggleExecution", ParallelCloud):
            controller.start(self.run_id, "lstm")
            controller.start(self.run_id, "gru")
            self.wait(controller)
            self.assertTrue(observed_cancel.is_set())
            self.assertEqual(len(transports), 2)
            self.assertIsNot(transports[0], transports[1])
            self.assertFalse(controller.operation_lock.acquire(blocking=False))
            controller.sweep()
        with self.sessions() as db:
            record = state.execution(db, self.run_id)
            self.assertTrue(record.cancel_requested)
            self.assertTrue(all(item["cleanup_confirmed"] for item in record.branches_json.values()))
            self.assertEqual(record.state, "failed")
        self.assertEqual(self.bridge.store.objects, {"models/active/preserve.pkl": b"preserve"})
        self.assertTrue(controller.operation_lock.acquire(blocking=False))
        controller.operation_lock.release()

    def test_failure_facts_keep_stage_and_type_without_exception_payload(self):
        try:
            raise FileNotFoundError('private-token-and-dataset-content')
        except FileNotFoundError as error:
            facts = failure_facts(error, 'prepare_notebook', 'tree')
        self.assertEqual(facts['error_type'], 'FileNotFoundError')
        self.assertEqual(facts['phase'], 'prepare_notebook')
        self.assertTrue(facts['frames'])
        self.assertNotIn('private-token', json.dumps(facts))
        self.assertNotIn(str(Path(__file__).parent), json.dumps(facts))

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
                {"training_backend": "kaggle", "_execution_protocol": 2, "selected_models": ["lstm", "gru"]}, "synthetic-dag")
            self.run_id = run.id
        def context(db, run_id):
            run = db.get(TrainingRun, run_id)
            return run, db.get(Dataset, run.dataset_id), None
        self.training = SimpleNamespace(_get_training_context=context,
            _ensure_dataset_downloaded=lambda dataset, run: self.dataset_path,
            get_settings=lambda: SimpleNamespace(cloud_training_enabled=True))
        self.bridge = FakeBridge()
        self.controllers = {}
        for role in ("tree", "recurrent"):
            controller = WorkerController(self.sessions, self.training, self.bridge, role=role,
                identity={"pod": "synthetic-" + role}, root=self.root / role,
                project=Path(__file__).resolve().parents[1], username='synthetic-user')
            self.controllers[role] = controller
        self.child_patch = patch("backend.app.services.training_control.KaggleExecution", FakeCloud)
        self.child_patch.start(); self.addCleanup(self.child_patch.stop)
        self.tracking_patch = patch("backend.app.services.training_tracking.TrainingTracker")
        tracker = self.tracking_patch.start(); tracker.return_value.cleanup.return_value = True
        self.addCleanup(self.tracking_patch.stop)

    def wait(self, controller):
        threads = [handle["thread"] for handle in controller.handles.values() if handle["thread"]]
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_only_tree_watcher_checks_unstarted_dispatch_and_throttles_airflow_reads(self):
        with patch('backend.app.services.training_control.time.monotonic', return_value=100), \
             patch('backend.app.services.training_control.state.reconcile_dispatch') as reconcile:
            self.controllers['recurrent'].sweep()
            reconcile.assert_not_called()
            self.controllers['tree'].sweep()
            self.controllers['tree'].sweep()
            reconcile.assert_called_once()
            self.assertEqual(reconcile.call_args.args[1], self.run_id)
        with patch('backend.app.services.training_control.time.monotonic', return_value=116), \
             patch('backend.app.services.training_control.state.reconcile_dispatch') as reconcile:
            self.controllers['tree'].sweep()
            reconcile.assert_called_once()

    def test_dispatch_watcher_reads_do_not_touch_cloud_or_artifact_cleanup(self):
        with patch('backend.app.services.airflow_client.get_dag_run') as read, \
             patch('backend.app.services.training_control.delete_prefix') as delete:
            self.controllers['tree'].sweep()
            read.assert_not_called()  # Fresh queued runs have a 15s grace period.
            delete.assert_not_called()
            self.assertEqual(self.bridge.store.objects, {'models/active/preserve.pkl': b'preserve'})

    def test_deployed_tree_environment_reconciles_failed_airflow_dispatch(self):
        from backend.app.core.config import Settings
        from backend.app.services import airflow_client

        manifest = json.loads((Path(__file__).resolve().parents[1]
            / 'deployment/rke2/roles/tree.json').read_text(encoding='utf-8'))
        worker = next(c for c in manifest['spec']['template']['spec']['containers'] if c['name'] == 'worker')
        environment = {e['name']: e['value'] for e in worker['env'] if 'value' in e}
        with patch.dict('os.environ', environment, clear=True):
            settings = Settings(_env_file=None, airflow_admin_username='synthetic',
                                airflow_admin_password='synthetic-password')
        with self.sessions() as db:
            run = db.get(TrainingRun, self.run_id)
            configuration = json.loads(json.dumps(run.configuration_json))
            document = {'dag_id': settings.training_dag_id, 'dag_run_id': run.airflow_dag_run_id,
                        'state': 'failed', 'conf': {'training_run_id': str(run.id),
                            'region_id': str(run.region_id), 'dataset_id': str(run.dataset_id)}}
            state.execution(db, self.run_id).created_at = datetime.now(UTC) - timedelta(minutes=1)
            db.commit()

        response = Mock(status_code=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [json.dumps(document).encode()]
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)

        def airflow_read(url, **kwargs):
            # There is no Airflow server on the worker's loopback interface.
            if url != ('http://airflow-webserver:8080/api/v1/dags/'
                       + settings.training_dag_id + '/dagRuns/' + document['dag_run_id']):
                raise airflow_client.requests.ConnectionError('Synthetic unreachable worker-local endpoint')
            return response

        session.get.side_effect = airflow_read
        with patch.object(airflow_client, 'get_settings', return_value=settings), \
             patch('backend.app.core.config.get_settings', return_value=settings), \
             patch.object(airflow_client.requests, 'Session', return_value=session), \
             patch('backend.app.services.training_control.delete_prefix') as delete:
            self.controllers['tree'].sweep()
            delete.assert_not_called()

        with self.sessions() as db:
            run = db.get(TrainingRun, self.run_id)
            visible = state.visible_runs(db, run.region_id)
            self.assertEqual(visible[0]['state'], 'failed')
            self.assertEqual(visible[0]['error_code'], 'dispatch_failed')
            self.assertEqual(run.status.value, 'failed')
            self.assertEqual(run.configuration_json, configuration)
            self.assertTrue(all(b['attempt_id'] is None for b in state.execution(db, run.id).branches_json.values()))
        session.get.assert_called_once()
        session.post.assert_not_called()
        self.assertFalse(session.trust_env)
        self.assertEqual(self.bridge.store.objects, {'models/active/preserve.pkl': b'preserve'})

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
            self.assertEqual(record.backend, "kaggle")
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

    def test_historical_local_record_cannot_start_a_new_attempt(self):
        controller = self.controllers['tree']
        with self.sessions() as db:
            run = db.get(TrainingRun, self.run_id)
            run.configuration_json = {**run.configuration_json, 'training_backend': 'local'}
            state.execution(db, self.run_id).backend = 'local'
            db.commit()
        with patch('backend.app.services.training_control.ExecutionProcess') as child:
            with self.assertRaisesRegex(Exception, 'Local training'):
                controller.start(self.run_id, 'tree')
            child.assert_not_called()
        with self.sessions() as db:
            self.assertIsNone(state.execution(db, self.run_id).branches_json['tree']['attempt_id'])


if __name__ == "__main__":
    unittest.main()
