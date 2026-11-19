"""Delete one published model's owned run files, with a durable retry journal.

Runs and drift history stay in the database. Dataset files and the region's
MLflow experiment are never deletion targets. Ambiguous legacy/shared paths
fail before the first mutation rather than claiming the disk was reclaimed.
"""
import re
import uuid

from sqlalchemy import select

from backend.app.core.errors import ApplicationError
from backend.app.db.models import AuditLog, DriftCheck, ModelVersion, Prediction, Region, TrainingRun
from backend.app.services.artifact_paths import owns_key
from backend.app.services.region_cleanup import (
    MODEL_COMPLETED, MODEL_PENDING, canonical_id, pending_cleanup, progress,
)
from backend.app.services.region_cleanup_resources import CleanupResources, fail
from backend.app.services.restore_cache import model_key

STEPS = ('local_files', 'mlflow', 'objects')


def local_prefixes(region_id, run_ids):
    rid = canonical_id(region_id)
    if len(run_ids) != 1:
        raise ValueError('Model cleanup requires exactly one training run')
    run = canonical_id(run_ids[0])
    return [f'models/distributed-serving/v1/{rid}/{run}/',
            f'models/distributed-training/v1/{run}/',
            f'models/regions/{rid}/training_runs/{run}/',
            f'models/regions/regions/{rid}/training_runs/{run}/']


def inventory(db, model):
    run = db.get(TrainingRun, model.training_run_id)
    if run is None or run.region_id != model.region_id or str(run.status.value) != 'completed':
        fail('The model training run is not completed. Nothing was deleted.', 'model_cleanup_conflict')
    if db.scalar(select(ModelVersion.id).where(ModelVersion.training_run_id == run.id,
                                              ModelVersion.id != model.id).limit(1)):
        fail('Another model shares this training output. Nothing was deleted.', 'model_cleanup_conflict')
    # Check all remaining model references, including preprocessor/SHAP paths
    # carried in training configuration, before purging a whole owned directory.
    prefixes = local_prefixes(model.region_id, [str(run.id)])
    try:
        if not owns_key(model_key(model.artifact_uri), prefixes):
            raise ValueError()
        for foreign in db.scalars(select(ModelVersion).where(ModelVersion.id != model.id)):
            other = db.get(TrainingRun, foreign.training_run_id)
            references = [foreign.artifact_uri, foreign.mlflow_model_uri,
                          (other.configuration_json if other else {}) or {}]
            def shared(value):
                if isinstance(value, dict): return any(shared(v) for v in value.values())
                if isinstance(value, list): return any(shared(v) for v in value)
                if not isinstance(value, str): return False
                path = value.removeprefix('file://').removeprefix('/app/')
                return owns_key(path, prefixes)
            if shared(references) or foreign.mlflow_run_id == model.mlflow_run_id:
                raise ValueError()
        foreign_links = [select(Region.id).where(Region.id != model.region_id, Region.active_model_version_id == model.id),
            select(TrainingRun.id).where(TrainingRun.region_id != model.region_id, TrainingRun.recommended_model_version_id == model.id),
            select(DriftCheck.id).where(DriftCheck.region_id != model.region_id, DriftCheck.active_model_version_id == model.id),
            select(Prediction.id).where(Prediction.region_id != model.region_id, Prediction.model_version_id == model.id)]
        if any(db.scalar(query.limit(1)) for query in foreign_links):
            raise ValueError()
    except ValueError:
        fail('Model file ownership is shared or uses an unsupported legacy layout. Nothing was deleted.',
             'model_cleanup_ownership_conflict')
    return {'version': 1, 'scope': 'model', 'region_id': str(model.region_id),
            'model_id': str(model.id), 'run_ids': [str(run.id)],
            'mlflow_run_id': model.mlflow_run_id, 'artifact_uri': model.artifact_uri,
            'local_prefixes': prefixes}


class ModelCleanupResources(CleanupResources):
    def preflight(self, db, plan):
        from mlflow.tracking import MlflowClient
        self.check_idle(plan)
        client = MlflowClient()
        entry = client.get_run(plan['mlflow_run_id'])
        tags = entry.data.tags
        run_id, experiment = entry.info.run_id, entry.info.experiment_id
        if (tags.get('traffic_training_run_id') != plan['run_ids'][0]
                or tags.get('traffic_region_id') != plan['region_id']
                or not re.fullmatch('[a-f0-9]{32}', run_id)
                or not experiment.isdigit()
                or entry.info.artifact_uri != f'mlflow-artifacts:/{experiment}/{run_id}/artifacts'
                or entry.info.status not in {'FINISHED', 'FAILED', 'KILLED'}):
            fail('The tracking run identity cannot be confirmed. Nothing was deleted.', 'model_cleanup_ownership_conflict')
        # Include previous deleted retries of the same publication, but refuse
        # another live model pointing at any one of those tracking runs.
        entries = client.search_runs([experiment],
            filter_string=f"tags.traffic_training_run_id = '{plan['run_ids'][0]}'",
            run_view_type=3, max_results=100)
        if len(entries) >= 100:
            fail('Tracking history exceeds the safe model cleanup budget.')
        tracked = []
        for item in entries:
            info, tags = item.info, item.data.tags
            if (tags.get('traffic_region_id') != plan['region_id']
                    or tags.get('region_id', plan['region_id']) != plan['region_id']
                    or tags.get('traffic_training_run_id') != plan['run_ids'][0]
                    or tags.get('training_run_id', plan['run_ids'][0]) != plan['run_ids'][0]
                    or info.experiment_id != experiment or not re.fullmatch('[a-f0-9]{32}', info.run_id)
                    or info.artifact_uri != f'mlflow-artifacts:/{experiment}/{info.run_id}/artifacts'
                    or info.status not in {'FINISHED', 'FAILED', 'KILLED'}
                    or db.scalar(select(ModelVersion.id).where(ModelVersion.id != uuid.UUID(plan['model_id']),
                        ModelVersion.mlflow_run_id == info.run_id).limit(1))):
                fail('Another model or active task owns tracking output.', 'model_cleanup_ownership_conflict')
            tracked.append(info.run_id)
        if run_id not in tracked:
            fail('The tracking inventory is incomplete. Nothing was deleted.')
        plan = {**plan, 'mlflow_runs': tracked, 'airflow': [], 'objects': [
            *[{'bucket': self.runtime_bucket, 'prefix': p} for p in plan['local_prefixes']],
            {'bucket': self.runtime_bucket, 'prefix':
             f"training-exchange/v1/regions/{plan['region_id']}/runs/{plan['run_ids'][0]}/"},
            *[{'bucket': self.mlflow_bucket, 'prefix': f'{experiment}/{identity}/'} for identity in tracked],
        ]}
        for target in plan['objects']:
            self.objects(target)
            self.multipart_uploads(target)
        return plan

    def clean(self, step, plan, journal_id):
        if step == 'mlflow':
            from mlflow.tracking import MlflowClient
            client = MlflowClient()
            for run_id in plan['mlflow_runs']:
                if client.get_run(run_id).info.lifecycle_stage != 'deleted':
                    client.delete_run(run_id)
        else:
            super().clean(step, plan, journal_id)

    def verify(self, plan, journal_id):
        super().verify(plan, journal_id)
        from mlflow.tracking import MlflowClient
        client = MlflowClient()
        if any(client.get_run(run_id).info.lifecycle_stage != 'deleted' for run_id in plan['mlflow_runs']):
            fail('Tracking cleanup is not confirmed. Retry deleting this model.')


def delete_model_version(db, model_id, *, backend=None):
    from backend.app.services.model_registry import get_model_version_or_404, _delete_model_records
    backend = backend if backend is not None else ModelCleanupResources()
    with backend.barrier(db):
        model = db.get(ModelVersion, model_id)
        if model is None:
            if db.scalar(select(AuditLog.id).where(AuditLog.entity_id == model_id,
                                                  AuditLog.action == MODEL_COMPLETED)):
                return
            get_model_version_or_404(db, model_id)
        journal = pending_cleanup(db)
        if journal and (journal.entity_id != model_id or journal.action != MODEL_PENDING):
            fail('Finish the previous model or region deletion before continuing.', 'model_cleanup_pending')
        if journal is None:
            plan = inventory(db, model)
            try:
                plan = backend.preflight(db, plan)
            except ApplicationError:
                raise
            except Exception:
                raise ApplicationError('model_cleanup_unavailable',
                    'Could not inspect model storage. Nothing was deleted; please try later.', 503) from None
            journal = AuditLog(action=MODEL_PENDING, entity_type='model', entity_id=model_id,
                after_json={'state': 'pending', 'phase': 'preparing', 'plan': plan, 'done': []})
            db.add(journal)
            db.commit()
        try:
            plan = journal.after_json['plan']
            backend.check_idle(plan)
            for step in STEPS:
                if step != 'local_files' and step in journal.after_json['done']:
                    continue
                progress(db, journal, step, state='pending')
                backend.clean(step, plan, str(journal.id))
                progress(db, journal, step, done=list(dict.fromkeys([*journal.after_json['done'], step])))
            progress(db, journal, 'verifying')
            backend.verify(plan, str(journal.id))
            _delete_model_records(db, model_id, commit=False)
            journal.action = MODEL_COMPLETED
            progress(db, journal, 'completed', state='completed')
        except Exception as error:
            db.rollback()
            journal = db.get(AuditLog, journal.id)
            progress(db, journal, journal.after_json.get('phase', 'preparing'),
                     state='retry_required', error_type=type(error).__name__)
            raise ApplicationError('model_cleanup_incomplete',
                'Model deletion is unfinished. Its progress is saved; retry Delete to continue safely.', 503) from None
