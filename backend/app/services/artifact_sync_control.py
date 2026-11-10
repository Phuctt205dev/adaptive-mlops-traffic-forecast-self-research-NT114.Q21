"""Versioned sync CLI and verified flush; stdout contains no artifact names."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import time

SCHEMA='traffic-artifact-sync/v1'
DIRECTORIES=('models','results','monitoring','mlruns','data_versions','data')


def internal(path):
    return '.traffic-cache' in Path(path).parts


def managed_cache(root):
    # Migration executes this checked helper inside a pinned legacy image.
    # Keep the non-opted-in protocol stdlib-only; that image cannot import
    # candidate modules. New native pods explicitly opt into the disk cache.
    if 'TRAFFIC_ARTIFACT_CACHE_BYTES' not in os.environ:return None
    from backend.app.services.artifact_cache import configured
    return configured(Path(root)/'models')


def capabilities():
    return {'schema':SCHEMA,'actions':['capabilities','flush'],'acknowledgement':'stable-file-inventory-v1'}


@contextmanager
def exclusive(*,budget=600):
    """Coordinate the uploader and an exec CLI in the same Linux container."""
    import fcntl
    with Path('/tmp/traffic-artifact-sync.lock').open('a') as lock:
        deadline=time.monotonic()+budget
        while True:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise RuntimeError('Artifact sync lock deadline exceeded') from None
                time.sleep(.2)
        try:yield
        finally:fcntl.flock(lock,fcntl.LOCK_UN)


def inventory(root,module):
    root=Path(root)
    if not root.is_dir() or root.is_symlink() or not (root/'models').is_dir():
        raise RuntimeError('Artifact cache mount is unavailable')
    # Reuse the exact deletion/tombstone policy of the running uploader. No
    # file excluded by an acknowledged region deletion is ever re-uploaded.
    with module.database_barrier(module.engine):
        with module.SessionLocal() as db:
            if module.pending_cleanup(db) is not None:
                raise RuntimeError('Artifact cleanup is active; flush is not confirmed')
            prefixes=[target['prefix'] for record in db.scalars(module.select(module.AuditLog).where(module.AuditLog.action==module.COMPLETED))
                      for target in record.after_json['plan']['objects']
                      if target['bucket']==module.os.environ['TRAFFIC_API_RUNTIME_BUCKET']]
    files={}
    for directory in DIRECTORIES:
        for path in (root/directory).rglob('*'):
            if not path.is_file() or path.is_symlink() or path.name.endswith('.tmp') or internal(path.relative_to(root)):
                continue
            if any(p.is_symlink() for p in path.parents if p!=root):continue
            key=path.relative_to(root).as_posix()
            if not module.owns_key(key,prefixes):files[key]=module.file_fingerprint(path)
    return files


def flush(root='/cache',*,module=None,state=None):
    cache=managed_cache(root)
    if cache:
        with cache.hold():return _flush(root,module=module,state=state)
    return _flush(root,module=module,state=state)


def _flush(root,*,module=None,state=None):
    if module is None:
        from backend.app.services import region_cleanup_uploader as module
    before=inventory(root,module)
    state=state if state is not None else module.UploadState()
    uploaded=module.upload_cycle(root,state=state)
    after=inventory(root,module)
    if before!=after:
        raise RuntimeError('Artifact cache changed during flush; retirement stopped')
    if any(state.acknowledged.get(key,(None,))[0]!=fingerprint for key,fingerprint in after.items()):
        raise RuntimeError('An artifact was not acknowledged; retirement stopped')
    return {'schema':SCHEMA,'complete':True,'stable':True,'acknowledged_files':len(after),'uploaded':uploaded}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['capabilities','flush'])
    parser.add_argument('--root',default='/cache')
    args=parser.parse_args(argv)
    try:
        if args.action=='capabilities':result=capabilities()
        else:
            with exclusive():result=flush(args.root)
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({'schema':SCHEMA,'complete':False,'error_type':type(error).__name__}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
