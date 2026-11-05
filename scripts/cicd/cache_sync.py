"""Load the release uploader after verifying the existing sidecar contract."""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys


def load_uploader(root=Path('/opt/traffic-release')):
    if os.environ.get('REGION_CLEANUP_ROLE') != 'sidecar':
        raise RuntimeError('The cache uploader requires an explicit sidecar role.')
    release = json.loads((root / 'release.json').read_text(encoding='utf-8'))
    source = root / 'sidecar/region_cleanup_uploader.py'
    if hashlib.sha256(source.read_bytes()).hexdigest() != release['sidecar_sha256']:
        raise RuntimeError('Release uploader identity differs.')
    spec = importlib.util.spec_from_file_location('delivery_sidecar_outer', '/opt/region-cleanup/region_cleanup_entrypoint.py')
    outer = importlib.util.module_from_spec(spec); spec.loader.exec_module(outer)
    outer.install_sources(outer.verify())
    parent = importlib.import_module('backend.app.services')
    name = 'backend.app.services.region_cleanup_uploader'
    if name in sys.modules:
        raise RuntimeError('Release uploader must be installed before its first import.')
    spec = importlib.util.spec_from_file_location(name, source)
    uploader = importlib.util.module_from_spec(spec)
    sys.modules[name] = uploader
    spec.loader.exec_module(uploader)
    setattr(parent, 'region_cleanup_uploader', uploader)
    return uploader


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-imports', action='store_true')
    args = parser.parse_args()
    uploader = load_uploader()
    if args.check_imports:
        print(json.dumps({'sidecar_imports_ready': True}), flush=True)
    else:
        uploader.main()
