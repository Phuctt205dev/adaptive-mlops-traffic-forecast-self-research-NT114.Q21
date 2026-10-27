"""Cache upload cycle coordinated with region deletion (also used by RKE2)."""
from pathlib import Path
from functools import lru_cache
import os
import time

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from sqlalchemy import select

from backend.app.db.models import AuditLog
from backend.app.db.session import engine, SessionLocal
from backend.app.services.region_cleanup import COMPLETED, database_barrier, pending_cleanup
from backend.app.services.region_cleanup_resources import owns_key


@lru_cache(maxsize=1)
def get_upload_client():
    # Preserve the existing sidecar's configuration. Its envFrom contains
    # MINIO_ROOT_* but not the API-only S3_ENDPOINT_URL/AWS_* overrides.
    return boto3.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL', 'http://minio:9000'),
        aws_access_key_id=os.environ['MINIO_ROOT_USER'],
        aws_secret_access_key=os.environ['MINIO_ROOT_PASSWORD'],
        region_name=os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'),
        config=Config(connect_timeout=5, read_timeout=30, retries={'max_attempts':2}))


def upload_cycle(root='/cache'):
    bucket = os.environ['TRAFFIC_API_RUNTIME_BUCKET']
    with database_barrier(engine):
        with SessionLocal() as db:
            if pending_cleanup(db) is not None:
                return 0
            prefixes = [target['prefix'] for record in db.scalars(select(AuditLog).where(AuditLog.action == COMPLETED))
                        for target in record.after_json['plan']['objects'] if target['bucket'] == bucket]
        client = get_upload_client()
        count = 0
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
                if owns_key(key, prefixes):
                    continue  # A replacement pod must never resurrect deleted files.
                client.upload_file(str(path), bucket, key,
                    Config=TransferConfig(max_concurrency=1, use_threads=False))
                count += 1
        return count


def main():
    while True:
        try:
            upload_cycle()
        except Exception:
            print('Artifact upload cycle deferred; no cleanup bypass.', flush=True)
        time.sleep(max(5, int(os.environ.get('TRAFFIC_API_ARTIFACT_SYNC_SECONDS', '30'))))
