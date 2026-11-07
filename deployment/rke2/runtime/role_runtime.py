"""Verified role entrypoint: no historical image overlay or local trainer."""
import hashlib
import json
import os
from pathlib import Path

ROOT = Path('/app')


def verify(root=ROOT):
    document = json.loads((root / 'role-release.json').read_text(encoding='utf-8'))
    identity = {k: v for k, v in document.items() if k not in {'source_id','commit'}}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != document.get('source_id'):
        raise RuntimeError('Role identity differs from its source and dependency contract')
    if document.get('schema') != 'traffic-role-runtime/v1' or document.get('role') not in {'api','tree','neural','sync'}:
        raise RuntimeError('Unknown role image manifest')
    for name, wanted in document['files'].items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise RuntimeError('Unsafe role source path')
        source = root / relative
        if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != wanted:
            raise RuntimeError('Role source differs from its release')
    if document['role'] != os.environ.get('TRAFFIC_IMAGE_ROLE'):
        raise RuntimeError('Image role and configured role differ')
    document['release_id'] = document['source_id']
    return document


def build_app(root=ROOT):
    document = verify(root)
    os.environ['TRAFFIC_ROLE_PEERS'] = 'routed'
    role = document['role']
    if role == 'api':
        from backend.app.services.serving_runtime import build_app as serving
        app = serving()
        from backend.app.services.lifecycle import install as lifecycle
        lifecycle(app)
        prefix = '/api/v1/health'
        from backend.app.api.routes.training_monitor import capabilities
        @app.get(prefix + '/cloud-training', include_in_schema=False)
        def cloud_health():
            c = capabilities()
            return {'status': 'ok', 'revision': 'cloud-training-v1', 'bundle_id': document['bundle_id'],
                    'execution_control': c['execution_control'], 'cloud_enabled': c['cloud_training'],
                    'local_training': False, 'neural_shap_supported': True}
        @app.get(prefix + '/shap', include_in_schema=False)
        def shap_health():
            return {'status': 'ok', 'feature_importance_ready': True, 'neural_shap_supported': True,
                    'revision': 'shap-system-v1', 'mode': 'api'}
        @app.get(prefix + '/model-quality', include_in_schema=False)
        def quality_health():
            return {'status': 'ok', 'revision': 'model-quality-v1', 'reference_source': 'dataset_traffic_volume',
                    'supported_hours': [24, 168, 336], 'default_hours': 168, 'training_execution_disabled': True}
    elif role in {'tree','neural'}:
        expected = 'tree' if role == 'tree' else 'recurrent'
        if os.environ.get('TRAFFIC_TRAINING_ROLE') != expected:
            raise RuntimeError('Worker role differs from its image')
        from backend.app.services.worker_runtime import build_app as worker
        app = worker(root / 'kaggle-project')
        app.state.cloud_bundle_id = document['bundle_id']
        prefix = '/health'
    else:
        raise RuntimeError('The sync image does not serve HTTP')
    from backend.app.services.region_cleanup_runtime import install
    peer_role = 'api' if role == 'api' else 'tree' if role == 'tree' else 'recurrent'
    install(app, role=peer_role, log_root='/opt/region-cleanup-airflow-logs' if role == 'neural' else None)
    @app.get(prefix + '/release', include_in_schema=False)
    def release():
        return {'status': 'ok', 'commit': document['commit'], 'release_id': document['release_id'],
                'bundle_id': document['bundle_id'], 'role': peer_role, 'image_role': role,
                'base_image': document['base_image'], 'runtime_schema': document['schema'], 'source_id': document['source_id'],
                'runtime_contract':'traffic-runtime-contract/v1'}
    return app


def main():
    document = verify()
    if document['role'] == 'sync':
        from backend.app.services.region_cleanup_uploader import main as sync
        return sync()
    import uvicorn
    uvicorn.run(build_app(), host='0.0.0.0', port=8000, workers=1, access_log=False,
                timeout_graceful_shutdown=850)


if __name__ == '__main__':
    main()
