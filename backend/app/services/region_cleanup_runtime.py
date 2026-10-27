"""Shared deployment barrier and narrow, token-protected local cleanup peers."""
import asyncio
import gc
import hmac
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

from fastapi import Header, HTTPException
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from backend.app.core.config import get_settings
from backend.app.core.errors import ApplicationError
from backend.app.db.models import AuditLog
from backend.app.db.session import engine, SessionLocal
from backend.app.services.region_cleanup import (
    COMPLETED, PENDING, canonical_id, database_barrier, local_prefixes, pending_cleanup,
)


DELETE_PATH = re.compile(r'^/api/v1/admin/regions/[0-9a-f-]{36}/?$')


def validated_directory(root, relative):
    root = Path(root).resolve(strict=True)
    parts = Path(relative).parts
    if Path(relative).is_absolute() or not parts or any(p in {'..', '.'} for p in parts):
        raise ValueError('Unsafe cleanup path')
    candidate = root.joinpath(*parts)
    # Check every component, not just the final symlink or a string prefix.
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Cleanup does not follow symbolic links')
    resolved = candidate.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError('Cleanup path escapes its root')
    if candidate.exists():
        if not candidate.is_dir():
            raise ValueError('Cleanup target must be a directory')
        for directory, dirs, files in os.walk(candidate, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in [*dirs, *files]):
                raise ValueError('Cleanup tree contains a symbolic link')
    return candidate


def clear_caches(app, plan):
    runtime = getattr(app.state, 'prediction_runtime', None)
    if runtime is not None:
        with runtime.lock:
            for key in list(runtime.models):
                path = str(key[0]).removeprefix('/app/')
                if any(path.startswith(p) for p in plan['local_prefixes']):
                    del runtime.models[key]
            for key in list(runtime.dashboards):
                if key[0] == plan['region_id']:
                    del runtime.dashboards[key]
    predictions = sys.modules.get('backend.app.services.predictions')
    if predictions is not None:
        # functools caches do not support removing a single key. Clearing them
        # only discards reproducible in-memory data; persistent regions remain.
        for name in ('_load_keras_model_from_path', '_load_neural_preprocessors',
                     '_download_dataset_frame_cached', '_hourly_context_cached'):
            function = getattr(predictions, name, None)
            seen = set()
            while function is not None and id(function) not in seen:
                seen.add(id(function))
                if hasattr(function, 'cache_clear'):
                    function.cache_clear()
                function = getattr(function, '__wrapped__', None)
    quality = sys.modules.get('backend.app.services.model_quality')
    if quality is not None:
        with quality._evaluation_lock:
            for key in list(quality._reports):
                if key[0] == plan['region_id']:
                    del quality._reports[key]
    gc.collect()  # Releases references; does not promise an immediate RSS drop.


def purge_local(app, plan, *, root='/app', log_root=None):
    expected = local_prefixes(plan['region_id'], plan['run_ids'])
    if plan['local_prefixes'] != expected:
        raise ValueError('Cleanup inventory paths differ from the region identity')
    paths = [validated_directory(root, prefix) for prefix in expected]
    if log_root:
        for run in plan.get('airflow', []):
            dag, run_id = run['dag_id'], run['run_id']
            if not re.fullmatch(r'[A-Za-z0-9_.-]+', dag) or any(v in run_id for v in ('/', '\\', '\x00')) or run_id in {'.', '..'}:
                raise ValueError('Unsafe Airflow log identity')
            paths.append(validated_directory(log_root, f'dag_id={dag}/run_id={run_id}'))
    # Validate the whole inventory first, then mutate only those directories.
    clear_caches(app, plan)
    removed = 0
    for path in paths:
        if path.exists():
            shutil.rmtree(path)
            removed += 1
    return removed


class CleanupBarrierMiddleware:
    def __init__(self, app, *, role):
        self.app, self.role = app, role

    async def __call__(self, scope, receive, send):
        path, method = scope.get('path', ''), scope.get('method')
        skip = (scope['type'] != 'http' or method == 'OPTIONS' or not path.startswith('/api/v1/')
                or path.startswith(('/api/v1/auth/', '/api/v1/health'))
                or (method == 'GET' and path.rstrip('/') in {'/api/v1/admin/regions', '/api/v1/regions'})
                or (self.role == 'api' and path.startswith('/api/v1/internal/training-runs'))
                or (method == 'DELETE' and DELETE_PATH.fullmatch(path)))
        if skip:
            return await self.app(scope, receive, send)
        barrier = database_barrier(engine)
        entered = False
        try:
            await run_in_threadpool(barrier.__enter__)
            entered = True
            def pending():
                with SessionLocal() as db:
                    return pending_cleanup(db) is not None
            if await run_in_threadpool(pending):
                raise ApplicationError('region_cleanup_pending', 'A region cleanup needs to finish. Retry Delete for that region before continuing.', 503)
            await self.app(scope, receive, send)
        except ApplicationError as error:
            response = JSONResponse({'error': {'code': error.code, 'message': error.message, 'details': {}}},
                                    status_code=error.status_code, headers={'Retry-After': '3'})
            await response(scope, receive, send)
        finally:
            if entered:
                await run_in_threadpool(barrier.__exit__, None, None, None)


def install(app, *, role='api', log_root=None):
    from backend.app.services import region_cleanup_resources
    if getattr(app.state, 'region_cleanup_installed', False):
        raise RuntimeError('Region cleanup is already installed')
    if role not in {'api', 'tree', 'recurrent'} or not os.environ.get('POD_NAME'):
        raise RuntimeError('Explicit region cleanup pod identity is required')
    if app.middleware_stack is not None:
        raise RuntimeError('Cleanup barrier must be installed before serving requests')
    async def token(x_internal_token: str | None = Header(default=None)):
        if not x_internal_token or not hmac.compare_digest(x_internal_token, get_settings().internal_training_token):
            raise HTTPException(403, 'Invalid internal token')
    from fastapi import Depends
    prefix = '/ops/region-cleanup'

    @app.get(prefix + '/health', dependencies=[Depends(token)], include_in_schema=False)
    def peer_health():
        # Shared DB locks are the authority for active work, including a train
        # whose HTTP client disconnected. This endpoint does not mutate data.
        return {'revision': 'region-cleanup-v1', 'role': role, 'pod': os.environ['POD_NAME'], 'busy': False}

    @app.post(prefix + '/{journal_id}', dependencies=[Depends(token)], include_in_schema=False)
    def cleanup_peer(journal_id: uuid.UUID):
        with SessionLocal() as db:
            journal = db.get(AuditLog, uuid.UUID(canonical_id(journal_id)))
            if journal is None or journal.action != PENDING or journal.entity_type != 'region':
                raise HTTPException(409, 'No pending cleanup inventory')
            plan = journal.after_json['plan']
            # Only a coordinator currently holding the exclusive DB barrier
            # may ask peers to remove files. A leaked/stale journal ID alone
            # is insufficient even with an authenticated internal request.
            try:
                with database_barrier(engine):
                    raise HTTPException(409, 'Cleanup coordinator is not active')
            except ApplicationError as error:
                if error.code != 'region_cleanup_busy':
                    raise
            removed = purge_local(app, plan, log_root=log_root)
            return {'pod': os.environ['POD_NAME'], 'removed_directories': removed}

    # Existing authentication/gates/CORS stay outside this barrier.
    app.user_middleware.append(Middleware(CleanupBarrierMiddleware, role=role))
    app.state.region_cleanup_installed = True
    region_cleanup_resources.INSTALLED = True

    async def sweep():
        # Completed tombstones also clean a pod that was replaced during an
        # earlier cleanup. The uploader separately skips these object prefixes.
        while True:
            try:
                def clean():
                    with SessionLocal() as db:
                        for record in db.scalars(select(AuditLog).where(AuditLog.action == COMPLETED)):
                            plan = record.after_json['plan']
                            if any((Path('/app') / prefix).exists() for prefix in plan['local_prefixes']):
                                purge_local(app, plan, log_root=log_root)
                await run_in_threadpool(clean)
            except Exception:
                # No arbitrary fallback paths, and no source/secret in logs.
                print('Region cleanup tombstone sweep will retry.', flush=True)
            await asyncio.sleep(30)

    @app.on_event('startup')
    async def start_sweep():
        app.state.region_cleanup_sweep = asyncio.create_task(sweep())

    @app.on_event('shutdown')
    async def stop_sweep():
        app.state.region_cleanup_sweep.cancel()
