"""Synthetic SQLite/files/S3 only: never delete production data for a test."""
from pathlib import Path
from contextlib import nullcontext
from tempfile import TemporaryDirectory
from types import SimpleNamespace as Obj
from unittest.mock import Mock, patch
import uuid
import unittest

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.core.errors import ApplicationError
from backend.app.db.models import AuditLog, DriftCheck, ModelVersion, Prediction, TrainingRun
from backend.app.services import model_cleanup as cleanup
from backend.app.services.region_cleanup import MODEL_COMPLETED, MODEL_PENDING, cleanup_status, pending_cleanup
from backend.app.services.region_cleanup_runtime import purge_local
from tests import test_region_cleanup as fixtures
from tests.test_region_cleanup import FakeResources


class ModelCleanupTests(unittest.TestCase):
    # Reuse the FK-enforced, two-region fixture, not its unrelated test cases.
    def setUp(self):
        fixtures.CleanupTests.setUp(self)
        self.model.artifact_uri = (f'models/distributed-serving/v1/{self.region.id}/{self.run.id}/bundle/model.pkl')
        self.db.commit()

    make_region = fixtures.CleanupTests.make_region
    tearDown = fixtures.CleanupTests.tearDown

    def test_complete_removes_files_and_records_but_preserves_dataset_history_and_other_region(self):
        model_id, rid, run_id = self.model.id, self.region.id, self.run.id
        backend = FakeResources()
        with TemporaryDirectory() as directory:
            owned = Path(directory) / self.model.artifact_uri
            protected = Path(directory) / f'models/distributed-serving/v1/{self.region.id}/{uuid.uuid4()}/model.pkl'
            for file in (owned, protected):
                file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(b'synthetic')
            original = backend.clean
            def clean(step, plan, journal):
                if step == 'local_files': purge_local(FastAPI(), plan, root=directory)
                original(step, plan, journal)
            backend.clean = clean
            cleanup.delete_model_version(self.db, model_id, backend=backend)
            self.assertFalse(owned.exists())
            self.assertTrue(protected.exists())
        self.assertIsNone(self.db.get(ModelVersion, model_id))
        self.assertIsNone(self.region.active_model_version_id)
        self.assertIsNone(self.run.recommended_model_version_id)
        self.assertIsNotNone(self.db.get(TrainingRun, run_id))
        self.assertEqual(len(list(self.db.scalars(select(DriftCheck).where(DriftCheck.region_id == rid)))), 1)
        self.assertEqual(len(list(self.db.scalars(select(Prediction).where(Prediction.region_id == rid)))), 0)
        self.assertIsNotNone(self.other.active_model_version_id)
        self.assertEqual(cleanup_status(self.db, model_id, 'model')['state'], 'completed')
        cleanup.delete_model_version(self.db, model_id, backend=backend)  # Lost HTTP reply is idempotent.
        self.assertEqual(backend.preflights, 1)

    def test_failed_object_delete_keeps_identity_and_resumes_without_repeating_tracking_delete(self):
        model_id = self.model.id
        backend = FakeResources('objects')
        with self.assertRaises(ApplicationError) as result:
            cleanup.delete_model_version(self.db, model_id, backend=backend)
        self.assertEqual(result.exception.code, 'model_cleanup_incomplete')
        self.assertIsNotNone(self.db.get(ModelVersion, model_id))
        saved = cleanup_status(self.db, model_id, 'model')
        self.assertEqual(saved['state'], 'retry_required')
        self.assertEqual(saved['phase'], 'objects')
        self.assertEqual(saved['done'], ['local_files', 'mlflow'])
        self.assertNotIn('plan', saved)
        self.assertNotIn('error_type', saved)
        self.assertIsNotNone(pending_cleanup(self.db))
        backend.failure = None; backend.calls.clear()
        cleanup.delete_model_version(self.db, model_id, backend=backend)
        self.assertEqual(backend.calls, ['local_files', 'objects', 'verify'])
        self.assertEqual(backend.preflights, 1)

    def test_same_run_shared_by_another_model_is_rejected_before_any_file_delete(self):
        other = self.db.scalar(select(ModelVersion).where(ModelVersion.region_id == self.other.id))
        other.training_run_id = self.run.id
        self.db.commit()
        backend = FakeResources()
        with self.assertRaises(ApplicationError): cleanup.delete_model_version(self.db, self.model.id, backend=backend)
        self.assertEqual(backend.preflights, 0)
        self.assertFalse(backend.calls)
        self.assertIsNone(pending_cleanup(self.db))

    def test_final_database_failure_keeps_model_and_saved_inventory_for_retry(self):
        model_id = self.model.id
        backend = FakeResources()
        commit = self.db.commit
        failed = False
        def commit_once():
            nonlocal failed
            if not failed and any(isinstance(row,AuditLog) and row.action == MODEL_COMPLETED
                                  for row in self.db.identity_map.values()):
                failed = True
                raise IntegrityError('synthetic', {}, Exception())
            return commit()
        with patch.object(self.db,'commit',side_effect=commit_once), self.assertRaises(ApplicationError):
            cleanup.delete_model_version(self.db, model_id, backend=backend)
        self.assertTrue(failed)
        self.assertIsNotNone(self.db.get(ModelVersion,model_id))
        self.assertEqual(self.region.active_model_version_id,model_id)
        self.assertEqual(cleanup_status(self.db,model_id,'model')['state'],'retry_required')
        cleanup.delete_model_version(self.db,model_id,backend=backend)
        self.assertIsNone(self.db.get(ModelVersion,model_id))

    def test_foreign_config_reference_and_unsafe_legacy_path_are_rejected(self):
        foreign = self.db.scalar(select(TrainingRun).where(TrainingRun.region_id == self.other.id))
        foreign.configuration_json = {'preprocessor_file': self.model.artifact_uri}
        self.db.commit()
        with self.assertRaises(ApplicationError): cleanup.inventory(self.db, self.model)
        foreign.configuration_json = {}; self.model.artifact_uri = 'models/shared/model.pkl'
        self.db.commit()
        with self.assertRaises(ApplicationError): cleanup.inventory(self.db, self.model)

    def test_preflight_outage_keeps_active_model_and_hides_private_details(self):
        backend = FakeResources()
        backend.preflight = Mock(side_effect=ConnectionError('private endpoint'))
        with self.assertRaises(ApplicationError) as failure:
            cleanup.delete_model_version(self.db, self.model.id, backend=backend)
        self.assertEqual(failure.exception.code, 'model_cleanup_unavailable')
        self.assertNotIn('private', failure.exception.message)
        self.assertEqual(self.region.active_model_version_id, self.model.id)
        self.assertIsNone(pending_cleanup(self.db))

    def test_replacement_uploader_and_flush_cannot_resurrect_deleted_model_output(self):
        from backend.app.services import region_cleanup_uploader as uploader
        from backend.app.services.artifact_sync_control import flush
        plan = cleanup.inventory(self.db, self.model)
        plan['objects'] = [{'bucket':'synthetic', 'prefix':p} for p in plan['local_prefixes']]
        self.db.add(AuditLog(action=MODEL_COMPLETED,entity_type='model',entity_id=self.model.id,
                            after_json={'state':'completed','plan':plan,'done':list(cleanup.STEPS)}))
        self.db.commit()
        client = Mock()
        with TemporaryDirectory() as root:
            owned = Path(root) / self.model.artifact_uri
            protected = Path(root) / f'models/distributed-serving/v1/{self.region.id}/{uuid.uuid4()}/model.pkl'
            for path in (owned,protected):
                path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(b'synthetic')
            state = uploader.UploadState()
            with patch.object(uploader,'database_barrier',side_effect=lambda *a,**k:nullcontext()), \
                 patch.object(uploader,'SessionLocal',side_effect=lambda:nullcontext(self.db)), \
                 patch.object(uploader,'get_upload_client',return_value=client), \
                 patch.object(uploader,'configured',return_value=None), \
                 patch.dict('os.environ',{'TRAFFIC_API_RUNTIME_BUCKET':'synthetic'}):
                self.assertEqual(uploader.upload_cycle(root,state=state),1)
                self.assertEqual(client.upload_file.call_args.args[2],protected.relative_to(root).as_posix())
                self.assertEqual(flush(root,module=uploader,state=state)['acknowledged_files'],1)
                self.assertNotIn(owned.relative_to(root).as_posix(),state.acknowledged)

    def test_pending_model_fences_region_deletion_too(self):
        backend = FakeResources('objects')
        with self.assertRaises(ApplicationError): cleanup.delete_model_version(self.db, self.model.id, backend=backend)
        from backend.app.services.region_cleanup import delete_region
        with self.assertRaises(ApplicationError) as result: delete_region(self.db, self.region.id, backend=backend)
        self.assertEqual(result.exception.code, 'region_cleanup_pending')

    def test_preflight_targets_one_tracking_run_and_never_the_region_experiment(self):
        plan = cleanup.inventory(self.db, self.model)
        resource = object.__new__(cleanup.ModelCleanupResources)
        resource.runtime_bucket, resource.mlflow_bucket = 'runtime', 'tracking'
        resource.check_idle, resource.objects, resource.multipart_uploads = Mock(), Mock(return_value=[]), Mock(return_value=[])
        rid = self.model.mlflow_run_id
        entry = Obj(info=Obj(run_id=rid, experiment_id='7', artifact_uri=f'mlflow-artifacts:/7/{rid}/artifacts',
                            status='FINISHED', lifecycle_stage='active'),
                    data=Obj(tags={'traffic_region_id':str(self.region.id), 'traffic_training_run_id':str(self.run.id)}))
        client = Mock(); client.get_run.return_value = entry; client.search_runs.return_value = [entry]
        with patch.dict('sys.modules', {'mlflow.tracking':Obj(MlflowClient=Mock(return_value=client))}):
            result = resource.preflight(self.db, plan)
            resource.clean('mlflow', result, 'test-journal')
        self.assertIn({'bucket':'tracking', 'prefix':f'7/{rid}/'}, result['objects'])
        self.assertNotIn({'bucket':'tracking', 'prefix':'7/'}, result['objects'])
        self.assertFalse(any('region_uploads' in item['prefix'] or item['prefix'].startswith('regions/') for item in result['objects']))
        client.delete_run.assert_called_once_with(rid)
        client.delete_experiment.assert_not_called()
        entry.data.tags['traffic_region_id'] = str(self.other.id)
        with patch.dict('sys.modules', {'mlflow.tracking':Obj(MlflowClient=Mock(return_value=client))}), self.assertRaises(ApplicationError):
            resource.preflight(self.db, plan)


class CleanupStatusAuthorizationTests(unittest.TestCase):
    def test_status_requires_admin_even_though_it_bypasses_the_cleanup_barrier(self):
        from fastapi.testclient import TestClient
        from backend.app.api.dependencies import get_current_user
        from backend.app.api.routes import model_versions, regions
        from backend.app.core.errors import register_exception_handlers
        from backend.app.db.models import UserRole
        from backend.app.db.session import get_db
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(regions.router,prefix='/api/v1/admin/regions')
        app.include_router(model_versions.router,prefix='/api/v1/admin')
        app.dependency_overrides[get_db] = lambda: None
        client = TestClient(app)
        for part in ('regions','model-versions'):
            path = f'/api/v1/admin/{part}/{uuid.uuid4()}/cleanup-status'
            self.assertEqual(client.get(path).status_code,401)
            app.dependency_overrides[get_current_user] = lambda: Obj(role=UserRole.USER)
            self.assertEqual(client.get(path).status_code,403)
            app.dependency_overrides[get_current_user] = lambda: Obj(role=UserRole.ADMIN)
            with patch('backend.app.services.region_cleanup.cleanup_status',return_value={'state':'pending','phase':'objects'}):
                response = client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertNotIn('plan',response.json())
            del app.dependency_overrides[get_current_user]


