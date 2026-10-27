"""Retryable region deletion: keep the inventory until every resource is removed.

Database and object storage cannot share one transaction. The existing audit
table therefore holds a small cleanup journal; it survives a crash/retry. The
region and its rows are removed LAST, not before their file locations are known.
"""
from contextlib import contextmanager
import uuid
from urllib.parse import urlsplit

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from backend.app.core.errors import ApplicationError
from backend.app.db.models import (
    AuditLog, Dataset, DriftCheck, ModelVersion, Prediction, Region, TrainingRun,
)


PENDING = 'region_cleanup_pending'
COMPLETED = 'region_cleanup_completed'
LOCK_KEY = 73918046215
STEPS = ('local_files', 'mlflow', 'airflow', 'objects')


def canonical_id(value):
    result = str(uuid.UUID(str(value)))
    if result != str(value):
        raise ValueError('Expected a canonical UUID')
    return result


def pending_cleanup(db, region_id=None):
    query = select(AuditLog).where(AuditLog.action == PENDING)
    if region_id is not None:
        query = query.where(AuditLog.entity_id == region_id)
    return db.scalars(query.order_by(AuditLog.created_at)).first()


@contextmanager
def database_barrier(engine, *, exclusive=False):
    """Separate connection keeps a session lock across journal commits.

    Normal requests and cache upload cycles take SHARED locks, so they can
    still run concurrently. A deletion takes an EXCLUSIVE lock, without waiting
    for or cancelling a running train/prediction/upload. Deployment enables
    this barrier on every API, both workers and both upload sidecars.
    """
    if engine.dialect.name != 'postgresql':
        raise ApplicationError('region_cleanup_unconfigured', 'Region cleanup requires its deployment configuration.', 503)
    from sqlalchemy import text
    suffix = '' if exclusive else '_shared'
    with engine.connect() as connection:
        acquired = connection.execute(text('SELECT pg_try_advisory_lock' + suffix + '(:key)'), {'key': LOCK_KEY}).scalar_one()
        connection.commit()
        if not acquired:
            raise ApplicationError('region_cleanup_busy', 'Training, prediction or cleanup is busy. Please try again shortly.', 409)
        try:
            yield
        finally:
            try:
                connection.execute(text('SELECT pg_advisory_unlock' + suffix + '(:key)'), {'key': LOCK_KEY})
                connection.commit()
            except Exception:
                connection.invalidate()  # Never return a locked session to the pool.
                raise


def local_prefixes(region_id, run_ids):
    region_id = canonical_id(region_id)
    runs = [canonical_id(value) for value in run_ids]
    # Exact directory boundaries, never a substring search or a region name.
    return [
        f'models/regions/{region_id}/',
        f'models/regions/regions/{region_id}/',  # Legacy tree pipeline.
        f'models/distributed-serving/v1/{region_id}/',
        f'models/shap-reports/v1/{region_id}/',
        f'data/region_uploads/{region_id}/',
        *[f'models/distributed-training/v1/{run}/' for run in runs],
    ]


def _inventory(db, region):
    datasets = list(db.scalars(select(Dataset).where(Dataset.region_id == region.id)))
    runs = list(db.scalars(select(TrainingRun).where(TrainingRun.region_id == region.id)))
    models = list(db.scalars(select(ModelVersion).where(ModelVersion.region_id == region.id)))
    dataset_ids, run_ids, model_ids = ([x.id for x in rows] for rows in (datasets, runs, models))
    # Unexpected cross-region references must be resolved before any file is
    # removed. A foreign key error at the final commit would be too late.
    checks = [
        select(TrainingRun.id).where(TrainingRun.region_id != region.id, TrainingRun.dataset_id.in_(dataset_ids)),
        select(ModelVersion.id).where(ModelVersion.region_id != region.id, ModelVersion.training_run_id.in_(run_ids)),
        select(Prediction.id).where(Prediction.region_id != region.id, Prediction.model_version_id.in_(model_ids)),
        select(Region.id).where(Region.id != region.id, Region.active_model_version_id.in_(model_ids)),
        select(TrainingRun.id).where(TrainingRun.region_id != region.id, TrainingRun.recommended_model_version_id.in_(model_ids)),
        select(DriftCheck.id).where(DriftCheck.region_id != region.id, or_(
            DriftCheck.dataset_id.in_(dataset_ids), DriftCheck.active_model_version_id.in_(model_ids),
            DriftCheck.triggered_training_run_id.in_(run_ids))),
    ]
    if any(db.scalar(query.limit(1)) is not None for query in checks):
        raise ApplicationError('region_cleanup_ownership_conflict', 'Another region still references this region data. Nothing was deleted.', 409)
    prefixes = local_prefixes(region.id, run_ids)
    dataset_prefix = f'regions/{region.id}/'
    for foreign in db.scalars(select(Dataset).where(Dataset.region_id != region.id)):
        if any(urlsplit(uri).path.lstrip('/').startswith(dataset_prefix)
               for uri in (foreign.storage_uri, foreign.quality_report_uri) if uri):
            raise ApplicationError('region_cleanup_ownership_conflict', 'Another region references these dataset files. Nothing was deleted.', 409)
    for foreign in db.scalars(select(ModelVersion).where(ModelVersion.region_id != region.id)):
        path = foreign.artifact_uri.removeprefix('file://').removeprefix('/app/')
        if any(path.startswith(prefix) for prefix in prefixes):
            raise ApplicationError('region_cleanup_ownership_conflict', 'Another region references these model files. Nothing was deleted.', 409)
    plan = {
        'version': 1, 'region_id': str(region.id), 'region_name': region.name,
        'run_ids': [str(x.id) for x in runs],
        'dataset_ids': [str(x.id) for x in datasets],
        'model_ids': [str(x.id) for x in models],
        'mlflow_run_ids': [x.mlflow_run_id for x in models],
        'airflow_run_ids': sorted({x.airflow_dag_run_id for x in runs if x.airflow_dag_run_id}),
        'dataset_uris': [uri for x in datasets for uri in (x.storage_uri, x.quality_report_uri) if uri],
        'artifact_uris': [x.artifact_uri for x in models],
        'local_prefixes': prefixes,
    }
    return plan


def _remove_records(db, region, journal):
    rid = region.id
    db.execute(update(Region).where(Region.id == rid).values(active_model_version_id=None))
    db.execute(update(TrainingRun).where(TrainingRun.region_id == rid).values(recommended_model_version_id=None))
    db.execute(delete(DriftCheck).where(DriftCheck.region_id == rid))
    db.execute(delete(Prediction).where(Prediction.region_id == rid))
    db.execute(delete(ModelVersion).where(ModelVersion.region_id == rid))
    db.execute(delete(TrainingRun).where(TrainingRun.region_id == rid))
    db.execute(delete(Dataset).where(Dataset.region_id == rid))
    db.delete(region)
    journal.action = COMPLETED
    journal.after_json = {**journal.after_json, 'state': 'completed'}
    db.commit()


def delete_region(db: Session, region_id: uuid.UUID, *, backend=None):
    if backend is None:
        from backend.app.services.region_cleanup_resources import CleanupResources
        backend = CleanupResources()
    with backend.barrier(db):
        region = db.get(Region, region_id)
        if region is None:
            completed = db.scalar(select(AuditLog.id).where(AuditLog.entity_id == region_id, AuditLog.action == COMPLETED))
            if completed:
                return  # A lost HTTP response can safely be retried.
            raise ApplicationError('region_not_found', 'Region was not found.', 404)
        journal = pending_cleanup(db)
        if journal is not None and journal.entity_id != region_id:
            raise ApplicationError('region_cleanup_pending', 'Finish deleting the previous region before deleting another region.', 409)
        if journal is None:
            plan = _inventory(db, region)
            try:
                plan = backend.preflight(db, plan)  # GET/list only. No deletion yet.
            except ApplicationError:
                raise
            except Exception:
                raise ApplicationError('region_cleanup_unavailable', 'Could not inspect region storage or training services. Nothing was deleted; please retry later.', 503) from None
            journal = AuditLog(action=PENDING, entity_type='region', entity_id=region_id,
                               after_json={'state': 'pending', 'plan': plan, 'done': []})
            region.is_active = False
            db.add(journal)
            db.commit()  # Persist the inventory BEFORE touching external stores.
        plan = journal.after_json['plan']
        try:
            backend.check_idle(plan)
            for step in STEPS:
                # Revisit local files on EVERY retry, including replacement pods.
                if step != 'local_files' and step in journal.after_json['done']:
                    continue
                backend.clean(step, plan, str(journal.id))
                done = list(dict.fromkeys([*journal.after_json['done'], step]))
                journal.after_json = {**journal.after_json, 'state': 'pending', 'done': done}
                db.commit()
            backend.verify(plan, str(journal.id))
            _remove_records(db, region, journal)
        except Exception as error:
            db.rollback()
            # Keep rows/locations and disable writes until an explicit retry.
            journal = db.get(AuditLog, journal.id)
            journal.after_json = {**journal.after_json, 'state': 'retry_required', 'error_type': type(error).__name__}
            db.commit()
            raise ApplicationError('region_cleanup_incomplete', 'Region cleanup is unfinished. Please retry Delete for this region; its cleanup progress has been saved.', 503) from None
