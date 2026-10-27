from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from backend.app.core.errors import ApplicationError
from backend.app.db.models import (
    AuditLog, Base, Dataset, DatasetStatus, DriftCheck, DriftCheckStatus,
    ModelVersion, ModelVersionStatus, Prediction, Region, TrainingRun,
    TrainingRunStatus, User, UserRole,
)
from backend.app.services import region_cleanup as cleanup
from backend.app.services.region_cleanup_resources import CleanupResources, owns_key
from backend.app.services.region_cleanup_runtime import CleanupBarrierMiddleware, install, purge_local, validated_directory


class FakeResources:
    def __init__(self, failure=None):
        self.failure, self.calls, self.preflights = failure, [], 0

    def barrier(self, db):
        return nullcontext()

    def preflight(self, db, plan):
        self.preflights += 1
        return {**plan, 'objects': [], 'airflow': []}

    def check_idle(self, plan):
        pass

    def clean(self, step, plan, journal_id):
        self.calls.append(step)
        if self.failure == step:
            raise RuntimeError('Test failure')

    def verify(self, plan, journal_id):
        self.calls.append('verify')


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        @event.listens_for(self.engine, 'connect')
        def foreign_keys(connection, record):
            connection.execute('PRAGMA foreign_keys=ON')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.user = User(email='cleanup@example.com', password_hash='test', role=UserRole.ADMIN)
        self.db.add(self.user)
        self.db.flush()
        self.region, self.dataset, self.run, self.model = self.make_region('A')
        self.other, _, _, _ = self.make_region('B')
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def make_region(self, name):
        region = Region(name=name, latitude=1, longitude=1, timezone='UTC')
        self.db.add(region)
        self.db.flush()
        dataset = Dataset(region_id=region.id, contract_version='test', original_filename='test.csv',
            storage_uri=f's3://traffic-datasets/regions/{region.id}/data.csv', sha256='a'*64,
            status=DatasetStatus.VALID, uploaded_by=self.user.id)
        self.db.add(dataset)
        self.db.flush()
        run = TrainingRun(region_id=region.id, dataset_id=dataset.id, status=TrainingRunStatus.COMPLETED,
            split_policy='test', requested_by=self.user.id)
        self.db.add(run)
        self.db.flush()
        model = ModelVersion(region_id=region.id, training_run_id=run.id, variant='RandomForest', version='v1',
            mlflow_run_id=uuid.uuid4().hex, mlflow_model_uri='runs:/test/model',
            artifact_uri=f'models/regions/{region.id}/model.pkl', cv_mean_mae=1, cv_std_mae=0, final_test_mae=1,
            status=ModelVersionStatus.ACTIVE)
        self.db.add(model)
        self.db.flush()
        region.active_model_version_id = model.id
        run.recommended_model_version_id = model.id
        self.db.add(DriftCheck(region_id=region.id, dataset_id=dataset.id, active_model_version_id=model.id,
            triggered_training_run_id=run.id, status=DriftCheckStatus.RETRAIN_TRIGGERED))
        self.db.add(Prediction(region_id=region.id, model_version_id=model.id, user_id=self.user.id,
            forecast_for=datetime.now(UTC), forecast_mode='future_time', prediction=1, feature_snapshot_json={}))
        return region, dataset, run, model

    def test_deletes_drift_and_dependencies_with_foreign_keys_enabled(self):
        backend = FakeResources()
        rid = self.region.id
        cleanup.delete_region(self.db, rid, backend=backend)
        self.assertIsNone(self.db.get(Region, rid))
        for model in (Dataset, TrainingRun, ModelVersion, Prediction, DriftCheck):
            self.assertEqual(len(list(self.db.scalars(select(model).where(model.region_id == rid)))), 0)
            self.assertEqual(len(list(self.db.scalars(select(model).where(model.region_id == self.other.id)))), 1)
        journal = self.db.scalar(select(AuditLog))
        self.assertEqual(journal.action, cleanup.COMPLETED)
        self.assertEqual(backend.calls, [*cleanup.STEPS, 'verify'])
        cleanup.delete_region(self.db, rid, backend=backend)
        self.assertEqual(backend.preflights, 1)

    def test_failure_keeps_inventory_and_retry_skips_completed_external_steps(self):
        backend = FakeResources('objects')
        with self.assertRaises(ApplicationError) as result:
            cleanup.delete_region(self.db, self.region.id, backend=backend)
        self.assertEqual(result.exception.code, 'region_cleanup_incomplete')
        self.assertIsNotNone(self.db.get(ModelVersion, self.model.id))
        self.assertFalse(self.db.get(Region, self.region.id).is_active)
        journal = cleanup.pending_cleanup(self.db)
        self.assertEqual(journal.after_json['state'], 'retry_required')
        self.assertEqual(journal.after_json['plan']['run_ids'], [str(self.run.id)])
        backend.failure = None
        backend.calls.clear()
        cleanup.delete_region(self.db, self.region.id, backend=backend)
        self.assertEqual(backend.calls, ['local_files', 'objects', 'verify'])
        self.assertEqual(backend.preflights, 1)

    def test_preflight_failure_changes_nothing(self):
        backend = FakeResources()
        backend.preflight = Mock(side_effect=ApplicationError('region_cleanup_busy', 'Busy', 409))
        with self.assertRaises(ApplicationError):
            cleanup.delete_region(self.db, self.region.id, backend=backend)
        self.assertTrue(self.region.is_active)
        self.assertIsNone(cleanup.pending_cleanup(self.db))
        self.assertEqual(backend.calls, [])

    def test_cross_region_model_reference_blocks_before_any_deletion(self):
        self.other.active_model_version_id = self.model.id
        self.db.commit()
        backend = FakeResources()
        with self.assertRaises(ApplicationError) as result:
            cleanup.delete_region(self.db, self.region.id, backend=backend)
        self.assertEqual(result.exception.code, 'region_cleanup_ownership_conflict')
        self.assertEqual(backend.preflights, 0)
        self.assertEqual(backend.calls, [])

    def test_pending_cleanup_blocks_deleting_another_region(self):
        backend = FakeResources('objects')
        with self.assertRaises(ApplicationError):
            cleanup.delete_region(self.db, self.region.id, backend=backend)
        with self.assertRaises(ApplicationError) as result:
            cleanup.delete_region(self.db, self.other.id, backend=backend)
        self.assertEqual(result.exception.code, 'region_cleanup_pending')

    def test_storage_inspection_outage_keeps_region_active(self):
        backend = FakeResources()
        backend.preflight = Mock(side_effect=ConnectionError('Private connection details'))
        with self.assertRaises(ApplicationError) as result:
            cleanup.delete_region(self.db,self.region.id,backend=backend)
        self.assertEqual(result.exception.code,'region_cleanup_unavailable')
        self.assertNotIn('Private',result.exception.message)
        self.assertTrue(self.region.is_active)
        self.assertIsNone(cleanup.pending_cleanup(self.db))

    def test_foreign_region_reusing_same_file_blocks_deletion(self):
        foreign = self.db.scalar(select(ModelVersion).where(ModelVersion.region_id==self.other.id))
        foreign.artifact_uri = self.model.artifact_uri
        self.db.commit()
        backend = FakeResources()
        with self.assertRaises(ApplicationError) as result:
            cleanup.delete_region(self.db,self.region.id,backend=backend)
        self.assertEqual(result.exception.code,'region_cleanup_ownership_conflict')
        self.assertEqual(backend.preflights,0)

    def test_storage_deletion_errors_are_not_reported_as_success(self):
        resource = object.__new__(CleanupResources)
        resource.client = Mock()
        resource.objects = Mock(return_value=[{'Key':'regions/test/file'}])
        resource.client.delete_objects.return_value = {'Errors': [{'Code': 'AccessDenied'}]}
        with self.assertRaises(ApplicationError):
            resource.clean('objects', {'objects': [{'bucket':'data','prefix':'regions/test/'}]}, 'id')

    def test_local_cleanup_preserves_other_region_and_shared_dependencies(self):
        plan = cleanup._inventory(self.db, self.region)
        with TemporaryDirectory() as tmp:
            owned = Path(tmp) / plan['local_prefixes'][0]
            other = Path(tmp) / f'models/regions/{self.other.id}'
            shared = Path(tmp) / 'models/shared'
            for p in (owned, other, shared):
                p.mkdir(parents=True)
                (p/'model.pkl').write_text('test')
            purge_local(FastAPI(), plan, root=tmp)
            self.assertFalse(owned.exists())
            self.assertTrue((other/'model.pkl').exists())
            self.assertTrue((shared/'model.pkl').exists())
            purge_local(FastAPI(), plan, root=tmp)  # Missing paths are safe on retry.

    def test_rejects_traversal_and_root_before_removing_files(self):
        with TemporaryDirectory() as tmp:
            for path in ('../outside', '/', '.', ''):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    validated_directory(tmp, path)

    def test_rejects_forged_local_inventory(self):
        plan = cleanup._inventory(self.db, self.region)
        plan['local_prefixes'] = ['models/']
        with TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            purge_local(FastAPI(), plan, root=tmp)

    def test_prefix_requires_region_directory_boundary(self):
        prefix = f'regions/{self.region.id}/'
        self.assertTrue(owns_key(prefix+'model.pkl', [prefix]))
        self.assertFalse(owns_key(prefix[:-1]+'-other/model.pkl', [prefix]))
        self.assertFalse(owns_key('shap-dependencies/shared.whl', [prefix]))

    def resource(self):
        resource = object.__new__(CleanupResources)
        resource.settings = SimpleNamespace(dataset_bucket='traffic-datasets', training_dag_id='traffic_region_training', drift_dag_id='traffic_region_drift_monitoring')
        resource.runtime_bucket = 'runtime'
        resource.mlflow_bucket = 'mlflow-artifacts'
        resource.check_idle = Mock()
        resource.list_airflow = Mock(return_value=[])
        resource.airflow = Mock(return_value=None)
        resource._mlflow = Mock(return_value=[])
        resource.objects = Mock(return_value=[])
        return resource

    def test_stale_queued_run_with_missing_airflow_is_inventory_not_retrained(self):
        self.run.status = TrainingRunStatus.QUEUED
        self.run.airflow_dag_run_id = 'old-failed-run'
        self.db.commit()
        plan = self.resource().preflight(self.db, cleanup._inventory(self.db, self.region))
        self.assertEqual(plan['airflow'], [{'dag_id':'traffic_region_training', 'run_id':'old-failed-run'}])
        self.assertEqual(self.run.status, TrainingRunStatus.QUEUED)
        self.assertTrue(self.region.is_active)
        self.assertIsNone(cleanup.pending_cleanup(self.db))

    def test_finds_failed_unregistered_run_by_airflow_region_identity(self):
        orphan = str(uuid.uuid4())
        resource = self.resource()
        resource.list_airflow.return_value = [{'dag_run_id':'failed-neural', 'conf':{'region_id':str(self.region.id), 'training_run_id':orphan}}]
        plan = resource.preflight(self.db, cleanup._inventory(self.db, self.region))
        self.assertIn(orphan, plan['run_ids'])
        self.assertIn(f'models/distributed-training/v1/{orphan}/', plan['local_prefixes'])

    def test_foreign_dataset_uri_is_rejected_before_any_external_delete(self):
        resource = self.resource()
        plan = cleanup._inventory(self.db, self.region)
        plan['dataset_uris'] = [f's3://traffic-datasets/regions/{self.other.id}/traffic.csv']
        with self.assertRaises(ApplicationError):
            resource.preflight(self.db, plan)
        resource.objects.assert_not_called()

    def test_legacy_invalid_run_identity_never_becomes_a_cleanup_path(self):
        resource = self.resource()
        resource.list_airflow.return_value = [{'dag_run_id':'old-manual-probe', 'conf':{
            'region_id':str(self.region.id), 'training_run_id':'../../unsafe'}}]
        plan = resource.preflight(self.db, cleanup._inventory(self.db, self.region))
        self.assertEqual(plan['noncanonical_airflow_conf'],1)
        self.assertEqual(plan['run_ids'],[str(self.run.id)])
        self.assertEqual(plan['airflow'],[{'dag_id':'traffic_region_training','run_id':'old-manual-probe'}])
        self.assertFalse(any('unsafe' in prefix for prefix in plan['local_prefixes']))

    def test_foreign_model_path_is_rejected(self):
        resource = self.resource()
        plan = cleanup._inventory(self.db, self.region)
        plan['artifact_uris'] = [f'/app/models/regions/{self.other.id}/model.pkl']
        with self.assertRaises(ApplicationError):
            resource.preflight(self.db, plan)

    def test_active_airflow_run_blocks_cleanup(self):
        resource = self.resource()
        resource.peers = Mock()
        resource.list_airflow.return_value = [{'state':'running'}]
        with self.assertRaises(ApplicationError) as result:
            CleanupResources.check_idle(resource, {})
        self.assertEqual(result.exception.code, 'region_cleanup_busy')

    def test_versioned_objects_include_all_versions_and_delete_markers(self):
        resource = self.resource()
        resource.client = Mock()
        resource.client.get_bucket_versioning.return_value = {'Status':'Enabled'}
        resource.client.get_paginator.return_value.paginate.return_value = [{
            'Versions':[{'Key':'regions/own/model','VersionId':'v1'}, {'Key':'regions/own/model','VersionId':'v2'}],
            'DeleteMarkers':[{'Key':'regions/own/model','VersionId':'marker'}]}]
        entries = CleanupResources.objects(resource, {'bucket':'data','prefix':'regions/own/'})
        self.assertEqual([item['VersionId'] for item in entries], ['v1','v2','marker'])
        resource.client.get_paginator.assert_called_once_with('list_object_versions')

    def test_peer_replacement_stops_final_database_removal(self):
        resource = self.resource()
        resource.peer_ids = ['before']
        resource.peers = Mock(return_value=[{'pod':'replacement'}])
        with self.assertRaises(ApplicationError):
            resource.verify({'objects':[], 'airflow':[]}, 'journal')

    def test_mlflow_mixed_region_experiment_is_rejected(self):
        class Page(list):
            token = None
        resource = self.resource()
        del resource._mlflow
        experiment = SimpleNamespace(name='Traffic Forecast - A', experiment_id='1', artifact_location='mlflow-artifacts:/1')
        records = Page([SimpleNamespace(data=SimpleNamespace(tags={'region_id':str(rid)}), info=SimpleNamespace(run_id=uuid.uuid4().hex)) for rid in (self.region.id,self.other.id)])
        client = Mock()
        client.search_experiments.return_value = [experiment]
        client.search_runs.return_value = records
        modules = {'mlflow':SimpleNamespace(), 'mlflow.entities':SimpleNamespace(ViewType=SimpleNamespace(ALL='ALL')),
                   'mlflow.tracking':SimpleNamespace(MlflowClient=Mock(return_value=client))}
        with patch.dict('sys.modules',modules), self.assertRaises(ApplicationError):
            resource._mlflow(self.db, cleanup._inventory(self.db, self.region))
        client.delete_experiment.assert_not_called()

    def test_pending_cleanup_keeps_region_list_and_retry_accessible(self):
        app = FastAPI()
        app.add_middleware(CleanupBarrierMiddleware,role='api')
        @app.get('/api/v1/admin/regions')
        def regions():
            return {'items':[]}
        @app.get('/api/v1/admin/datasets/test')
        def dataset():
            return {}
        @app.delete('/api/v1/admin/regions/{rid}')
        def retry(rid:str):
            return {'retry':True}
        client = TestClient(app)
        with patch('backend.app.services.region_cleanup_runtime.database_barrier',return_value=nullcontext()), \
             patch('backend.app.services.region_cleanup_runtime.SessionLocal') as session:
            session.return_value.__enter__.return_value = self.db
            with patch('backend.app.services.region_cleanup_runtime.pending_cleanup',return_value=object()):
                self.assertEqual(client.get('/api/v1/admin/regions').status_code,200)
                self.assertEqual(client.get('/api/v1/admin/datasets/test').status_code,503)
                self.assertEqual(client.delete('/api/v1/admin/regions/'+str(self.region.id)).status_code,200)

    def test_cleanup_peer_rejects_unauthenticated_purge_before_db(self):
        app = FastAPI()
        with patch.dict('os.environ',{'POD_NAME':'unit-api'}), \
             patch('backend.app.services.region_cleanup_resources.INSTALLED',False), \
             patch('backend.app.services.region_cleanup_runtime.get_settings',return_value=SimpleNamespace(internal_training_token='unit-token')):
            install(app)
            client = TestClient(app)
            with patch('backend.app.services.region_cleanup_runtime.SessionLocal') as session:
                self.assertEqual(client.get('/ops/region-cleanup/health').status_code,403)
                self.assertEqual(client.post('/ops/region-cleanup/'+str(uuid.uuid4())).status_code,403)
                self.assertEqual(client.get('/ops/region-cleanup/health',headers={'X-Internal-Token':'unit-token'}).status_code,200)
                session.assert_not_called()

    def test_uploader_preserves_minio_sidecar_environment_without_api_overrides(self):
        from backend.app.services.region_cleanup_uploader import get_upload_client
        get_upload_client.cache_clear()
        try:
            with patch.dict('os.environ',{'MINIO_ROOT_USER':'unit-user','MINIO_ROOT_PASSWORD':'unit-secret'},clear=True), \
                 patch('backend.app.services.region_cleanup_uploader.boto3.client') as create:
                get_upload_client()
                self.assertEqual(create.call_args.kwargs['endpoint_url'],'http://minio:9000')
                self.assertEqual(create.call_args.kwargs['aws_access_key_id'],'unit-user')
                self.assertEqual(create.call_args.kwargs['aws_secret_access_key'],'unit-secret')
        finally:
            get_upload_client.cache_clear()


if __name__ == '__main__':
    unittest.main()
