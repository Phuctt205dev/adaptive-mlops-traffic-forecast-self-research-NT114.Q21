"""Verified outer wrapper; preserve SHAP, serving and worker source guards."""
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import sys


ROOT = Path('/opt/region-cleanup')
REVISION = 'region-cleanup-v1'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify(root=ROOT):
    manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    identity = {k:v for k,v in manifest.items() if k != 'bundle_id'}
    if manifest['revision'] != REVISION or manifest['bundle_id'] != hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest():
        raise RuntimeError('Unexpected cleanup manifest identity')
    for name, expected in manifest['files'].items():
        if Path(name).name != name or sha(root/name) != expected:
            raise RuntimeError('Cleanup source differs from review')
    role = os.environ['REGION_CLEANUP_ROLE']
    if role not in {'api','tree','recurrent','sidecar'}:
        raise RuntimeError('Explicit cleanup role is required')
    for path, expected in manifest['original_files'][role].items():
        if not path.startswith(('/app/','/opt/traffic-shap/','/opt/traffic-training/','/opt/traffic-quality/','/opt/traffic-runtime/')) or sha(path) != expected:
            raise RuntimeError('Original image/runtime differs from review')
    return manifest


class Sources(importlib.abc.MetaPathFinder):
    def __init__(self, paths):
        self.paths = paths

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.paths:
            return importlib.util.spec_from_file_location(fullname, self.paths[fullname])


def install_sources(manifest, root=ROOT):
    paths = {}
    for module, name in manifest['modules'].items():
        if module in sys.modules or Path(name).name != name or name not in manifest['files']:
            raise RuntimeError('Cleanup modules must be installed before backend imports')
        paths[module] = root/name
    sys.path.insert(0,'/app')
    sys.meta_path.insert(0,Sources(paths))


def build_app(root=ROOT):
    manifest = verify(root)
    install_sources(manifest, root)
    role = os.environ['REGION_CLEANUP_ROLE']
    if role == 'sidecar':
        raise RuntimeError('Sidecar does not serve an API')
    base_path = '/opt/traffic-training/training_worker_execution.py' if role=='recurrent' else '/opt/traffic-shap/shap_system_runtime.py'
    sys.path.insert(0,str(Path(base_path).parent))
    spec = importlib.util.spec_from_file_location('cleanup_base_entrypoint',base_path)
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    app = base.build_app()
    from backend.app.services.region_cleanup_runtime import install
    log_root = '/opt/region-cleanup-airflow-logs' if role=='recurrent' else None
    install(app,role=role,log_root=log_root)
    path = '/api/v1/health/region-cleanup' if role=='api' else '/health/region-cleanup'

    @app.get(path,include_in_schema=False)
    def health():
        return {'status':'ok','revision':REVISION,'bundle_id':manifest['bundle_id'],'role':role}

    app.openapi_schema = None
    return app


if __name__=='__main__':
    if os.environ.get('REGION_CLEANUP_ROLE')=='sidecar':
        manifest = verify()
        install_sources(manifest)
        from backend.app.services.region_cleanup_uploader import main
        main()
    else:
        import uvicorn
        uvicorn.run(build_app(),host='0.0.0.0',port=8000,workers=1,access_log=False)
