"""Restore bounded, safe artifact paths using the lightweight sync image."""
import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath

DIRECTORIES = ('models', 'results', 'monitoring', 'mlruns', 'data_versions', 'data')
MAX_BYTES = 4 * 1024**3 * 4 // 5
MAX_FILES = 20000


class RestoreBudgetExceeded(RuntimeError):
    def __init__(self, report):
        self.report = report
        super().__init__('Artifact cache exceeds the reviewed restore budget: '
                         f"files={report['files']}/{report['max_files']}, "
                         f"bytes={report['bytes']}/{report['max_bytes']}")


def inventory(client, bucket, *, max_bytes=MAX_BYTES, max_files=MAX_FILES):
    """Bounded listing shared by preflight/init. No object keys in the report.

    On overflow counts are observed lower bounds. Init lists again because
    preflight cannot reserve space or prevent subsequent object writes.
    """
    if type(max_bytes) is not int or max_bytes < 0 or type(max_files) is not int or max_files < 0:
        raise ValueError('Invalid restore budget')
    entries, total = [], 0
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket):
        for item in page.get('Contents', []):
            key = item['Key']
            parts = PurePosixPath(key).parts
            if not parts or parts[0] not in DIRECTORIES or key.endswith('/'):
                continue
            if key.startswith('/') or '\\' in key or any(p in {'', '.', '..'} for p in key.split('/')):
                raise RuntimeError('Unsafe artifact cache key')
            size = item['Size']
            if type(size) is not int or size < 0:
                raise ValueError('Invalid artifact object size')
            entries.append((key, size))
            total += size
            report = {'schema': 'traffic-restore-inventory/v1', 'files': len(entries), 'bytes': total,
                      'max_files': max_files, 'max_bytes': max_bytes, 'complete': False,
                      'within_budget': len(entries) <= max_files and total <= max_bytes}
            if not report['within_budget']:
                raise RestoreBudgetExceeded(report)
    return entries, {'schema': 'traffic-restore-inventory/v1', 'files': len(entries), 'bytes': total,
                     'max_files': max_files, 'max_bytes': max_bytes, 'complete': True, 'within_budget': True}


def inventory_report(client, bucket, **budgets):
    try:
        return inventory(client, bucket, **budgets)[1]
    except RestoreBudgetExceeded as error:
        return error.report


def prepare(root):
    for name in DIRECTORIES:
        (root / name).mkdir(parents=True, exist_ok=True)


def restore(root, *, max_bytes=MAX_BYTES):
    from contextlib import closing
    from backend.app.services.region_cleanup_uploader import get_upload_client
    client = get_upload_client()
    bucket = os.environ['TRAFFIC_API_RUNTIME_BUCKET']
    prepare(root)
    entries, _ = inventory(client, bucket, max_bytes=max_bytes)
    for key, size in entries:
        path = root.joinpath(*PurePosixPath(key).parts)
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise RuntimeError('Cache restore cannot follow symlinks')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.restore.tmp')
        response = client.get_object(Bucket=bucket, Key=key)
        sha, count = hashlib.sha256(), 0
        try:
            with closing(response['Body']) as body, temporary.open('wb') as output:
                while block := body.read(256 * 1024):
                    count += len(block)
                    if count > size:
                        raise RuntimeError('Artifact cache object exceeded its listed size')
                    sha.update(block)
                    output.write(block)
            expected = response.get('Metadata', {}).get('sha256')
            if count != size or expected and expected != sha.hexdigest():
                raise RuntimeError('Artifact cache object differs from its storage metadata')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    print('artifact_cache_restore_verified=true; files=' + str(len(entries)), flush=True)


if __name__ == '__main__':
    import signal
    import sys
    # Init containers run this process as PID 1. Honour pod cancellation so a
    # failed staging attempt does not wait for the full serving grace period.
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(143))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'restore', 'wait'])
    parser.add_argument('--root', type=Path, default=Path('/cache'))
    parser.add_argument('--host')
    parser.add_argument('--port',type=int)
    args = parser.parse_args()
    if args.action=='wait':
        import socket,time
        if not args.host or not args.port:parser.error('wait requires host and port')
        print('runtime_dependency_waiting',flush=True)
        deadline=time.monotonic()+180
        while True:
            try:
                with socket.create_connection((args.host,args.port),timeout=3):break
            except OSError:
                if time.monotonic()>=deadline:raise RuntimeError('Runtime service did not become reachable') from None
                time.sleep(3)
    else:
        (restore if args.action == 'restore' else prepare)(args.root)
