"""Resource adapters for the region cleanup journal. No broad bucket deletion."""
import os
from pathlib import PurePosixPath
import socket
import uuid
from urllib.parse import quote, urlsplit

import requests
from botocore.exceptions import ClientError
from sqlalchemy import select

from backend.app.core.config import get_settings
from backend.app.core.errors import ApplicationError
from backend.app.db.models import ModelVersion, Region, TrainingRun
from backend.app.services.region_cleanup import canonical_id, database_barrier, local_prefixes
from backend.app.services.storage import get_s3_client
from src.mlflow_regions import region_experiment_name, legacy_region_experiment_name


INSTALLED = False
PEER_HOST = 'traffic-region-cleanup-peers'
MAX_OBJECTS = 20000


def fail(message, code='region_cleanup_preflight_failed'):
    raise ApplicationError(code, message, 409)


def owns_key(key, prefixes):
    return isinstance(key, str) and any(key.startswith(prefix) for prefix in prefixes)


class CleanupResources:
    def __init__(self):
        self.settings = get_settings()
        self.http = requests.Session()
        self.http.trust_env = False
        self.client = get_s3_client()
        self.runtime_bucket = os.environ.get('TRAFFIC_API_RUNTIME_BUCKET', '')
        self.mlflow_bucket = os.environ.get('REGION_CLEANUP_MLFLOW_BUCKET', '')

    def barrier(self, db):
        if not INSTALLED or not self.runtime_bucket or not self.mlflow_bucket:
            raise ApplicationError('region_cleanup_unconfigured', 'Region cleanup is not deployed yet. No data was deleted.', 503)
        self.check_idle({})  # Reject active Airflow work before requesting priority.
        return database_barrier(db.get_bind(), exclusive=True)

    def airflow(self, method, dag, run=None):
        path = '/dags/' + quote(dag, safe='') + '/dagRuns'
        if run is not None:
            path += '/' + quote(run, safe='')
        response = self.http.request(method, self.settings.airflow_api_url.rstrip('/') + path,
            auth=(self.settings.airflow_admin_username, self.settings.airflow_admin_password), timeout=(3, 15))
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            fail('Airflow could not confirm cleanup. No tasks were cancelled.')
        return response.json() if response.content else None

    def list_airflow(self, dag):
        records = []
        offset = 0
        while True:
            response = self.http.get(self.settings.airflow_api_url.rstrip('/') + '/dags/' + quote(dag, safe='') + '/dagRuns',
                params={'limit': 100, 'offset': offset},
                auth=(self.settings.airflow_admin_username, self.settings.airflow_admin_password), timeout=(3, 15))
            if response.status_code != 200:
                fail('Airflow history is unavailable. Please retry later.')
            page = response.json()
            records.extend(page['dag_runs'])
            offset += len(page['dag_runs'])
            if offset >= page['total_entries']:
                return records
            if not page['dag_runs'] or offset >= 10000:
                fail('Airflow history is too large for a safe cleanup.')

    def peers(self):
        addresses = sorted({r[4][0] for r in socket.getaddrinfo(PEER_HOST, 8000, family=socket.AF_INET, type=socket.SOCK_STREAM)})
        peers = []
        for address in addresses:
            base = f'http://{address}:8000/ops/region-cleanup'
            response = self.http.get(base + '/health', headers={'X-Internal-Token': self.settings.internal_training_token}, timeout=(3, 8))
            if response.status_code != 200:
                fail('A region cleanup peer is unavailable. Please retry later.')
            data = response.json()
            if data.get('revision') != 'region-cleanup-v1' or data.get('busy') is not False:
                fail('A worker is busy or cleanup deployment is incomplete.')
            peers.append({'url': base, 'pod': data['pod'], 'role': data['role']})
        roles = sorted(p['role'] for p in peers)
        if roles != ['api', 'api', 'recurrent', 'tree'] or len({p['pod'] for p in peers}) != 4:
            fail('All two APIs and two workers must be available before deleting a region.')
        return peers

    def check_idle(self, plan):
        self.peers()
        # Global DAGs can choose the region after starting; reject them while
        # active. Completed global drift history is shared and stays intact.
        for dag in (self.settings.training_dag_id, self.settings.drift_dag_id):
            if any(r.get('state') not in {'success', 'failed'} for r in self.list_airflow(dag)):
                fail('Airflow has active work. Wait for it to finish before deleting a region.', 'region_cleanup_busy')

    def _mlflow(self, db, plan):
        from mlflow.entities import ViewType
        from mlflow.tracking import MlflowClient
        client = MlflowClient()
        rid = plan['region_id']
        names = {region_experiment_name(plan['region_name'], rid), legacy_region_experiment_name(rid)}
        other_names = {region_experiment_name(r.name, r.id) for r in db.scalars(select(Region).where(Region.id != uuid.UUID(rid)))}
        known = {m.mlflow_run_id: str(m.region_id) for m in db.scalars(select(ModelVersion))}
        selected = []
        experiments = []
        token = None
        while True:
            page = client.search_experiments(view_type=ViewType.ALL, max_results=1000, page_token=token)
            experiments.extend(page)
            token = getattr(page, 'token', None)
            if not token:
                break
            if len(experiments) >= 10000:
                fail('Too many MLflow experiments for a safe cleanup.')
        for exp in experiments:
            runs = []
            token = None
            while True:
                page = client.search_runs([exp.experiment_id], run_view_type=ViewType.ALL, max_results=1000, page_token=token)
                runs.extend(page)
                token = page.token
                if not token:
                    break
                if len(runs) >= 10000:
                    fail('MLflow history is too large for a safe cleanup.')
            owners = [r.data.tags.get('region_id') or known.get(r.info.run_id) for r in runs]
            if exp.name not in names and rid not in owners:
                continue
            if exp.name in other_names or any(owner not in {None, '', rid} for owner in owners):
                fail('MLflow experiment ownership is ambiguous. Nothing was deleted.', 'region_cleanup_ownership_conflict')
            # Untagged legacy neural runs are allowed only in an unambiguous
            # region-named experiment. Renamed/mixed experiments fail closed.
            if exp.name not in names and any(not owner for owner in owners):
                fail('Some MLflow runs have no region identity. Nothing was deleted.', 'region_cleanup_ownership_conflict')
            if not exp.experiment_id.isdigit() or exp.artifact_location != f'mlflow-artifacts:/{exp.experiment_id}':
                fail('Unsupported MLflow artifact location. Nothing was deleted.')
            if any(not r.info.artifact_uri.startswith(exp.artifact_location + '/') for r in runs):
                fail('An MLflow run stores files outside its experiment. Nothing was deleted.')
            selected.append({'id': exp.experiment_id, 'run_ids': [r.info.run_id for r in runs]})
        return selected

    def preflight(self, db, plan):
        self.check_idle(plan)
        rid = plan['region_id']
        run_ids = set(plan['run_ids'])
        dag = self.settings.training_dag_id
        history = self.list_airflow(dag)
        owned = set(plan['airflow_run_ids'])
        for run in history:
            conf = run.get('conf') or {}
            if str(conf.get('region_id')) == rid or str(conf.get('training_run_id')) in run_ids:
                owned.add(run['dag_run_id'])
                if conf.get('training_run_id'):
                    try:
                        identifier = canonical_id(conf['training_run_id'])
                    except (ValueError, TypeError, AttributeError):
                        # Old manual/security probes can contain arbitrary text.
                        # Region-owned Airflow metadata is still inventory, but
                        # this value must never become a filesystem/S3 prefix.
                        plan['noncanonical_airflow_conf'] = plan.get('noncanonical_airflow_conf', 0) + 1
                        continue
                    registered = db.get(TrainingRun, uuid.UUID(identifier))
                    if registered is not None and str(registered.region_id) != rid:
                        fail('Airflow training identity belongs to another region.', 'region_cleanup_ownership_conflict')
                    run_ids.add(identifier)
        plan['run_ids'] = sorted(run_ids)
        plan['local_prefixes'] = local_prefixes(rid, plan['run_ids'])
        plan['airflow'] = []
        for run_id in sorted(owned):
            run = self.airflow('GET', dag, run_id)
            if run is not None:
                conf = run.get('conf') or {}
                if conf.get('region_id') and str(conf['region_id']) != rid:
                    fail('Airflow run ownership conflicts with the selected region.', 'region_cleanup_ownership_conflict')
                if run.get('state') not in {'success', 'failed'}:
                    fail('A training run is still active.', 'region_cleanup_busy')
            # A stale DB queued row whose Airflow run is missing/terminal may
            # be deleted with its region; never mark it successful to bypass a gate.
            plan['airflow'].append({'dag_id': dag, 'run_id': run_id})
        prefix = f'regions/{rid}/'
        for uri in plan['dataset_uris']:
            parts = urlsplit(uri)
            if parts.scheme != 's3' or parts.netloc != self.settings.dataset_bucket or not parts.path.lstrip('/').startswith(prefix):
                fail('Dataset ownership cannot be confirmed.', 'region_cleanup_ownership_conflict')
        for uri in plan['artifact_uris']:
            path = uri.removeprefix('file://').removeprefix('/app/')
            if '..' in PurePosixPath(path).parts or not owns_key(path, plan['local_prefixes']):
                fail('Model file ownership cannot be confirmed.', 'region_cleanup_ownership_conflict')
        plan['mlflow'] = self._mlflow(db, plan)
        plan['objects'] = [
            {'bucket': self.settings.dataset_bucket, 'prefix': prefix},
            *[{'bucket': self.runtime_bucket, 'prefix': p} for p in plan['local_prefixes']],
            {'bucket': self.runtime_bucket, 'prefix': f'training-exchange/v1/regions/{rid}/'},
            *[{'bucket': self.mlflow_bucket, 'prefix': exp['id'] + '/'} for exp in plan['mlflow']],
        ]
        for target in plan['objects']:
            self.objects(target)  # Validate listing permissions/limits before mutation.
            self.multipart_uploads(target)
        return plan

    def multipart_uploads(self, target):
        uploads = []
        for page in self.client.get_paginator('list_multipart_uploads').paginate(Bucket=target['bucket'], Prefix=target['prefix']):
            uploads.extend({'Key': item['Key'], 'UploadId': item['UploadId']} for item in page.get('Uploads', []))
            if len(uploads) > MAX_OBJECTS:
                fail('Too many unfinished uploads for a safe cleanup.')
        if any(not item['Key'].startswith(target['prefix']) for item in uploads):
            fail('An unfinished upload is outside the selected region.')
        return uploads

    def objects(self, target):
        keys = []
        versioned = self.client.get_bucket_versioning(Bucket=target['bucket']).get('Status') in {'Enabled', 'Suspended'}
        operation = 'list_object_versions' if versioned else 'list_objects_v2'
        for page in self.client.get_paginator(operation).paginate(Bucket=target['bucket'], Prefix=target['prefix']):
            if versioned:
                keys.extend({'Key': item['Key'], 'VersionId': item['VersionId']}
                            for item in [*page.get('Versions', []), *page.get('DeleteMarkers', [])])
            else:
                keys.extend({'Key': item['Key']} for item in page.get('Contents', []))
            if len(keys) > MAX_OBJECTS:
                fail('Too many files for a safe cleanup. Contact the administrator.')
        if not all(item['Key'].startswith(target['prefix']) for item in keys):
            fail('Storage returned a file outside the selected region.')
        return keys

    def clean(self, step, plan, journal_id):
        if step == 'local_files':
            peers = self.peers()
            for peer in peers:
                response = self.http.post(peer['url'] + '/' + journal_id,
                    headers={'X-Internal-Token': self.settings.internal_training_token}, timeout=(3, 30))
                if response.status_code != 200 or response.json().get('pod') != peer['pod']:
                    fail('Local cleanup is incomplete. Retry deleting this region.')
            self.peer_ids = sorted(p['pod'] for p in peers)
        elif step == 'mlflow':
            from mlflow.tracking import MlflowClient
            client = MlflowClient()
            for exp in plan['mlflow']:
                if client.get_experiment(exp['id']).lifecycle_stage != 'deleted':
                    client.delete_experiment(exp['id'])
        elif step == 'airflow':
            for run in plan['airflow']:
                current = self.airflow('GET', run['dag_id'], run['run_id'])
                if current is not None:
                    if current.get('state') not in {'success', 'failed'}:
                        fail('An Airflow run became active. Cleanup stopped.')
                    self.airflow('DELETE', run['dag_id'], run['run_id'])
        elif step == 'objects':
            for target in plan['objects']:
                for upload in self.multipart_uploads(target):
                    try:
                        self.client.abort_multipart_upload(Bucket=target['bucket'], **upload)
                    except ClientError as error:
                        if error.response.get('Error', {}).get('Code') != 'NoSuchUpload':
                            raise
                keys = self.objects(target)
                for start in range(0, len(keys), 1000):
                    result = self.client.delete_objects(Bucket=target['bucket'], Delete={
                        'Objects': keys[start:start+1000], 'Quiet': True})
                    if result.get('Errors'):
                        fail('Some region files could not be deleted. Please retry.')

    def verify(self, plan, journal_id):
        if self.peer_ids != sorted(p['pod'] for p in self.peers()):
            fail('A pod changed during cleanup. Retry deleting this region.')
        for target in plan['objects']:
            if self.objects(target) or self.multipart_uploads(target):
                fail('Some region files remain. Retry deleting this region.')
        for run in plan['airflow']:
            if self.airflow('GET', run['dag_id'], run['run_id']) is not None:
                fail('Airflow cleanup is incomplete. Retry deleting this region.')
