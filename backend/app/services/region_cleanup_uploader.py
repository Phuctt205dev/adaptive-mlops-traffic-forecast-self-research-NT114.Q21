"""Cache upload cycle coordinated with region deletion (also used by RKE2)."""
from pathlib import Path
from functools import lru_cache
import json
import os
import time

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from sqlalchemy import select

from backend.app.db.models import AuditLog
from backend.app.db.session import engine, SessionLocal
from backend.app.services.region_cleanup import COMPLETED, cleanup_requested, database_barrier, pending_cleanup
from backend.app.services.artifact_paths import owns_key


@lru_cache(maxsize=1)
def get_upload_client():
    # Preserve the existing sidecar's configuration. Its envFrom contains
    # MINIO_ROOT_* but not the API-only S3_ENDPOINT_URL/AWS_* overrides.
    return boto3.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL', 'http://minio:9000'),
        aws_access_key_id=os.environ['MINIO_ROOT_USER'],
        aws_secret_access_key=os.environ['MINIO_ROOT_PASSWORD'],
        region_name=os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'),
        config=Config(connect_timeout=5, read_timeout=30, retries={'max_attempts':2}))


class UploadPaused(Exception):
    """Ask S3Transfer to abort its own upload before releasing the shared lock."""


class YieldToCleanup:
    def __init__(self, connection):
        self.connection = connection
        self.last_check = float('-inf')

    def __call__(self, transferred):
        now = time.monotonic()
        if now - self.last_check >= 0.5:
            self.last_check = now
            if cleanup_requested(self.connection):
                raise UploadPaused()


class UploadState:
    """Remember only acknowledged, stable files; restart safely reuploads."""
    def __init__(self, reconcile_seconds=3600, clock=time.monotonic):
        self.reconcile_seconds, self.clock = reconcile_seconds, clock
        self.acknowledged = {}
        self.unchanged = 0

    def needs_upload(self, key, fingerprint):
        previous = self.acknowledged.get(key)
        return previous is None or previous[0] != fingerprint or self.clock() - previous[1] >= self.reconcile_seconds

    def remember(self, key, fingerprint):
        self.acknowledged[key] = (fingerprint, self.clock())


def file_fingerprint(path):
    info = path.stat(follow_symlinks=False)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def upload_cycle(root='/cache', *, state=None):
    state = state if state is not None else UploadState()
    state.unchanged = 0
    bucket = os.environ['TRAFFIC_API_RUNTIME_BUCKET']
    client = get_upload_client()
    count, seen = 0, set()
    for directory in ('models', 'results', 'monitoring', 'mlruns', 'data_versions', 'data'):
        parent = Path(root) / directory
        if not parent.exists():
            continue
        for path in parent.rglob('*'):
            if not path.is_file() or path.is_symlink() or path.name.endswith('.tmp'):
                continue
            if any(p.is_symlink() for p in path.parents if p != Path(root)):
                continue
            key = path.relative_to(root).as_posix()
            seen.add(key)
            try:
                fingerprint = file_fingerprint(path)
            except FileNotFoundError:
                continue
            # Skipping an unchanged file performs no writes, so it needs no
            # DB barrier. Reconciliation still rechecks cleanup before upload.
            if not state.needs_upload(key, fingerprint):
                state.unchanged += 1
                continue
            # Each object is protected until success or multipart abort, but a
            # whole 1+ GiB cache cycle can no longer starve an admin deletion.
            with database_barrier(engine) as connection:
                with SessionLocal() as db:
                    if pending_cleanup(db) is not None:
                        return count
                    prefixes = [target['prefix'] for record in db.scalars(select(AuditLog).where(AuditLog.action == COMPLETED))
                                for target in record.after_json['plan']['objects'] if target['bucket'] == bucket]
                if not path.is_file() or path.is_symlink() or path.name.endswith('.tmp'):
                    continue
                if any(p.is_symlink() for p in path.parents if p != Path(root)):
                    continue
                key = path.relative_to(root).as_posix()
                if owns_key(key, prefixes):
                    state.acknowledged.pop(key, None)
                    continue  # A replacement pod must never resurrect deleted files.
                try:
                    fingerprint = file_fingerprint(path)
                except FileNotFoundError:
                    continue
                client.upload_file(str(path), bucket, key,
                    Config=TransferConfig(max_concurrency=1, use_threads=False), Callback=YieldToCleanup(connection))
                count += 1
                try:
                    stable = file_fingerprint(path) == fingerprint
                except FileNotFoundError:
                    stable = False
                if stable:
                    state.remember(key, fingerprint)
                else:
                    state.acknowledged.pop(key, None)
    for key in state.acknowledged.keys() - seen:
        del state.acknowledged[key]
    return count


def main():
    import signal
    import threading
    from backend.app.services.artifact_sync_control import exclusive,flush
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    state = UploadState(reconcile_seconds=max(300, int(os.environ.get('TRAFFIC_API_ARTIFACT_RECONCILE_SECONDS', '3600'))))
    while not stop.is_set():
        try:
            with exclusive():
                uploaded = upload_cycle(state=state)
            print(json.dumps({'artifact_sync': {'uploaded': uploaded, 'unchanged': state.unchanged}}), flush=True)
        except UploadPaused:
            pass
        except Exception:
            print('Artifact upload cycle deferred; no cleanup bypass.', flush=True)
        stop.wait(max(5, int(os.environ.get('TRAFFIC_API_ARTIFACT_SYNC_SECONDS', '30'))))
    try:
        with exclusive():
            proof=flush(module=__import__(__name__,fromlist=['upload_cycle']),state=state)
        print(json.dumps({'artifact_sync_stopped': True, 'final_flush':proof}), flush=True)
    except Exception:
        print(json.dumps({'artifact_sync_stopped': True, 'final_upload_confirmed': False}), flush=True)
        raise
