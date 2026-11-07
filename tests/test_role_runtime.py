import copy
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.cicd import roles_release
from backend.app.services.lifecycle import install as install_lifecycle
from backend.app.services.worker_runtime import WorkerGate
from backend.app.services.training_artifacts import BranchArtifacts, ArtifactError
from backend.app.services.training_result_bridge import TrainingResultBridge

ROOT = Path(__file__).resolve().parents[1]


class RolePackageTests(unittest.TestCase):
    def test_roles_exclude_unrelated_executors_and_uploader_has_no_ml_source(self):
        api = roles_release.closure(ROOT, 'api')
        worker = roles_release.closure(ROOT, 'neural')
        sync = roles_release.closure(ROOT, 'sync')
        self.assertNotIn('backend/app/services/training_control.py', api)
        self.assertIn('src/regression_metrics.py', api)
        self.assertNotIn('src/models/registry.py', worker)
        self.assertNotIn('backend/app/api/routes/datasets.py', worker)
        self.assertFalse(any(name.startswith('src/') for name in sync))
        self.assertNotIn('backend/app/core/errors.py', sync)
        self.assertNotIn('backend/app/api/router.py', sync)

    def test_content_ids_ignore_commit_but_detect_role_specific_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            for name in ('backend', 'src', 'scripts', 'deployment'):
                for source in (ROOT / name).rglob('*.py'):
                    if '__pycache__' in source.parts:
                        continue
                    target = root / source.relative_to(ROOT)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
            for name in ('deployment/rke2/role-baseline.json', 'docs/index.html'):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            shutil.copytree(ROOT / 'requirements/roles', root / 'requirements/roles')
            shutil.copytree(ROOT / 'deployment/rke2/roles', root / 'deployment/rke2/roles')
            shutil.copytree(ROOT / 'deployment/rke2/migrations', root / 'deployment/rke2/migrations')
            shutil.copy2(ROOT / 'deployment/rke2/runtime-contract.json', root / 'deployment/rke2/runtime-contract.json')
            a = roles_release.build(root, Path(directory) / 'a', 'a' * 40)
            b = roles_release.build(root, Path(directory) / 'b', 'b' * 40)
            self.assertNotEqual(a['release_id'], b['release_id'])
            self.assertEqual([a['roles'][r]['source_id'] for r in roles_release.ROLES],
                             [b['roles'][r]['source_id'] for r in roles_release.ROLES])
            manifest=root/'deployment/rke2/roles/api.json'
            value=json.loads(manifest.read_text());value['spec']['template']['spec']['containers'][0]['readinessProbe']['timeoutSeconds']=7
            manifest.write_text(json.dumps(value))
            configuration=roles_release.build(root,Path(directory)/'configuration','d'*40)
            self.assertNotEqual(b['release_id'],configuration['release_id'])
            self.assertEqual([b['roles'][r]['source_id'] for r in roles_release.ROLES],
                             [configuration['roles'][r]['source_id'] for r in roles_release.ROLES])
            source = root / 'backend/app/services/worker_context.py'
            source.write_text(source.read_text(encoding='utf-8') + '\n# worker-only change\n', encoding='utf-8')
            changed = roles_release.build(root, Path(directory) / 'changed', 'c' * 40)
            for role in ('api', 'sync'):
                self.assertEqual(a['roles'][role]['source_id'], changed['roles'][role]['source_id'])
            for role in ('tree', 'neural'):
                self.assertNotEqual(a['roles'][role]['source_id'], changed['roles'][role]['source_id'])
            file = Path(directory) / 'changed/roles/tree/backend/app/services/worker_context.py'
            file.write_bytes(b'changed after review')
            with self.assertRaisesRegex(ValueError, 'source differs'):
                roles_release.verify(Path(directory) / 'changed')


class WorkerBoundaryTests(unittest.TestCase):
    def client(self, role):
        app = FastAPI()
        app.add_middleware(WorkerGate, role=role, token='test-token')
        @app.post('/api/v1/internal/training-runs/{run}/control/{branch}/start')
        def reached(run, branch):
            return {'reached': True}
        return TestClient(app)

    def test_auth_and_role_gate_runs_before_request_parsing(self):
        client = self.client('tree')
        self.addCleanup(client.close)
        path = '/api/v1/internal/training-runs/' + str(uuid.UUID(int=1))
        self.assertEqual(client.post(path + '/control/tree/start', content='not json').status_code, 403)
        headers = {'X-Internal-Token': 'test-token'}
        self.assertEqual(client.post(path + '/execute/tree', headers=headers).status_code, 409)
        self.assertEqual(client.post(path + '/control/lstm/start', headers=headers).status_code, 409)
        self.assertEqual(client.post(path + '/control/tree/start', headers=headers).status_code, 200)

    def test_neural_worker_rejects_tree_and_finalization(self):
        client = self.client('recurrent')
        self.addCleanup(client.close)
        path = '/api/v1/internal/training-runs/' + str(uuid.UUID(int=1))
        headers = {'X-Internal-Token': 'test-token'}
        self.assertEqual(client.post(path + '/control/tree/start', headers=headers).status_code, 409)
        self.assertEqual(client.post(path + '/control/finalize', headers=headers).status_code, 409)


class DrainTests(unittest.TestCase):
    def test_draining_withdraws_readiness_but_completes_inflight_business_request(self):
        app = FastAPI()
        with patch('backend.app.core.config.get_settings', return_value=SimpleNamespace(internal_training_token='test-token')):
            install_lifecycle(app)
        entered, release = threading.Event(), threading.Event()
        @app.get('/work')
        async def work():
            import asyncio
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            return {'complete': True}
        client = TestClient(app)
        self.addCleanup(client.close)
        responses = []
        thread = threading.Thread(target=lambda: responses.append(client.get('/work')))
        thread.start()
        self.assertTrue(entered.wait(3))
        try:
            self.assertEqual(app.state.serving_lifecycle.inflight, 1)
            response = client.post('/api/v1/internal/lifecycle/drain', headers={'X-Internal-Token': 'test-token'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['inflight'], 1)
            self.assertEqual(client.get('/api/v1/health/serving-ready').status_code, 503)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(app.state.serving_lifecycle.inflight, 0)


class MemoryS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, Metadata, **kwargs):
        raw = Body.read() if hasattr(Body, 'read') else Body
        self.objects[(Bucket, Key)] = {'raw': raw, 'metadata': Metadata.copy()}

    def head_object(self, *, Bucket, Key):
        value = self.objects[(Bucket, Key)]
        return {'ContentLength': len(value['raw']), 'Metadata': value['metadata']}

    def get_object(self, *, Bucket, Key):
        value = self.objects[(Bucket, Key)]
        return {'Body': io.BytesIO(value['raw']), 'ContentLength': len(value['raw'])}


class PortedArtifactContractTests(unittest.TestCase):
    def setup_bridge(self, directory):
        root = Path(directory)
        for name in ('source', 'receive', 'models', 'models/serving'):
            (root / name).mkdir(parents=True, exist_ok=True)
        store = BranchArtifacts(MemoryS3(), 'synthetic', max_total_bytes=8192)
        bridge = TrainingResultBridge(store, artifact_root=root / 'source', receive_root=root / 'receive',
                                      model_root=root / 'models', serving_root=root / 'models/serving')
        run = SimpleNamespace(id=uuid.UUID(int=1), region_id=uuid.UUID(int=2), dataset_id=uuid.UUID(int=3),
                              configuration_json={'training_backend':'kaggle','_execution_protocol':2,'selected_models':['random_forest_lag']})
        model = root / 'source/tree/model.bin'
        model.parent.mkdir()
        model.write_bytes(b'synthetic-model')
        compatibility = model.with_name('compatibility.npz')
        compatibility.write_bytes(b'synthetic-compatibility')
        candidate = {'best_model_name':'RandomForest','model_family':'legacy_sklearn','model_file':str(model),
                     'compatibility_file':str(compatibility),'validation_MAE':1.0}
        result = {'branch':'tree','status':'completed','candidates':[candidate], 'production_candidate':copy.deepcopy(candidate)}
        receipts = {'tree':bridge.publish_branch(run,'tree',result)}
        for branch in ('lstm','gru'):
            receipts[branch] = bridge.publish_branch(run,branch,{'branch':branch,'status':'skipped','candidates':[]})
        return bridge, run, receipts

    def test_compatibility_artifact_is_hash_bound_and_rebound_with_model(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge, run, receipts = self.setup_bridge(directory)
            with bridge.finalize_scope(run, receipts):
                result = bridge.read_branch(run, 'tree')
                candidate = result['candidates'][0]
                self.assertEqual(Path(candidate['model_file']).read_bytes(), b'synthetic-model')
                self.assertEqual(Path(candidate['compatibility_file']).read_bytes(), b'synthetic-compatibility')
                self.assertEqual(bridge.read_branch(run, 'lstm')['status'], 'skipped')

    def test_wrong_run_receipt_is_rejected_before_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge, run, receipts = self.setup_bridge(directory)
            receipts['tree']['run_id'] = str(uuid.UUID(int=8))
            with self.assertRaises(ArtifactError):
                with bridge.finalize_scope(run, receipts):
                    self.fail('Foreign output must not enter finalization')


if __name__ == '__main__':
    unittest.main()
