"""Restore bounded, safe artifact paths using the lightweight sync image."""
import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath

DIRECTORIES = ('models', 'results', 'monitoring', 'mlruns', 'data_versions', 'data')


def prepare(root):
    for name in DIRECTORIES:
        (root / name).mkdir(parents=True, exist_ok=True)


def restore(root, *, max_bytes=4 * 1024**3 * 4 // 5):
    from contextlib import closing
    from backend.app.services.region_cleanup_uploader import get_upload_client
    client = get_upload_client()
    bucket = os.environ['TRAFFIC_API_RUNTIME_BUCKET']
    prepare(root)
    entries = []
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket):
        for item in page.get('Contents', []):
            key = item['Key']
            parts = PurePosixPath(key).parts
            if not parts or parts[0] not in DIRECTORIES or key.endswith('/'):
                continue
            if key.startswith('/') or '\\' in key or any(p in {'','.','..'} for p in key.split('/')):
                raise RuntimeError('Unsafe artifact cache key')
            entries.append((key, int(item['Size'])))
            if len(entries) > 20000 or sum(size for _, size in entries) > max_bytes:
                raise RuntimeError('Artifact cache exceeds the reviewed restore budget')
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'restore'])
    parser.add_argument('--root', type=Path, default=Path('/cache'))
    args = parser.parse_args()
    (restore if args.action == 'restore' else prepare)(args.root)
