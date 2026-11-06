"""Tracked serving cache/queue contract ported from the deployed runtime.

The release manifest verifies this source before application imports.
"""

import asyncio
import copy
import functools
import gc
import hashlib
import importlib
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

PROTECTED = re.compile(r"^/api/v1/regions/[^/]+/(predictions|forecast-dashboard|prediction-window)/?$")


class PredictionRuntime:
    def __init__(self, error_class, loader, *, root="/app/models", client=None,
                 max_models=2, file_budget=900 * 1024 * 1024, dashboard_ttl=30):
        self.error_class = error_class
        self.loader = loader
        self.root = Path(root).resolve()
        self.client = client
        self.lock = threading.RLock()
        self.models = OrderedDict()
        self.dashboards = OrderedDict()
        self.max_models = max_models
        # Serialized file sizes are a cache admission estimate, not a RAM limit.
        self.file_budget = file_budget
        self.dashboard_ttl = dashboard_ttl

    def _s3(self):
        if self.client is None:
            import boto3
            from botocore.config import Config
            self.client = boto3.client(
                "s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL", "http://minio:9000"),
                aws_access_key_id=os.environ["MINIO_ROOT_USER"],
                aws_secret_access_key=os.environ["MINIO_ROOT_PASSWORD"],
                region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
                config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}),
            )
        return self.client

    def ensure_file(self, filename):
        path = Path(filename).resolve()
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            raise self.error_class("model_artifact_path_invalid", "Active model path is invalid.", 500)
        if not relative.parts:
            raise self.error_class("model_artifact_path_invalid", "Active model path is invalid.", 500)
        with self.lock:
            if path.is_file() and path.stat().st_size > 0:
                return path
            key = "models/" + relative.as_posix()
            temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                from boto3.s3.transfer import TransferConfig
                bucket = os.environ["TRAFFIC_API_RUNTIME_BUCKET"]
                client = self._s3()
                head = client.head_object(Bucket=bucket, Key=key)
                if head["ContentLength"] <= 0:
                    raise ValueError("Empty object")
                path.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(bucket, key, str(temporary), Config=TransferConfig(
                    max_concurrency=1, use_threads=False, io_chunksize=256 * 1024,
                ))
                if temporary.stat().st_size != head["ContentLength"]:
                    raise ValueError("Incomplete download")
                os.replace(temporary, path)
                print("PREDICTION ARTIFACT RESTORED:", key, flush=True)
            except Exception as error:
                print("PREDICTION ARTIFACT RESTORE FAILED:", key, type(error).__name__, flush=True)
                raise self.error_class(
                    "model_artifact_unavailable",
                    "The active model is temporarily unavailable. Please try again shortly.", 503,
                ) from None
            finally:
                temporary.unlink(missing_ok=True)
            return path

    def load_model(self, filename):
        with self.lock:
            path = self.ensure_file(filename)
            stat = path.stat()
            key = (str(path), stat.st_mtime_ns, stat.st_size)
            if key in self.models:
                self.models.move_to_end(key)
                return self.models[key][0]
            # Evict old versions of the same path and make space BEFORE loading.
            for old in list(self.models):
                if old[0] == key[0]:
                    del self.models[old]
            evicted = False
            while self.models and (
                len(self.models) >= self.max_models
                or sum(item[1] for item in self.models.values()) + stat.st_size > self.file_budget
            ):
                self.models.popitem(last=False)
                evicted = True
            if evicted:
                gc.collect()
            model = self.loader(str(path))
            if stat.st_size <= self.file_budget:
                self.models[key] = (model, stat.st_size)
            print("PREDICTION MODEL LOADED:", path.name, "cached=", len(self.models), flush=True)
            return model

    def serialized(self, function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            with self.lock:
                return function(*args, **kwargs)
        return wrapped

    def dashboard(self, predictions, function):
        @functools.wraps(function)
        def wrapped(db, region_id):
            with self.lock:
                region = db.get(predictions.Region, region_id)
                if region is None or not region.is_active or not region.active_model_version_id:
                    return function(db, region_id)
                model = db.get(predictions.ModelVersion, region.active_model_version_id)
                if model is None:
                    return function(db, region_id)
                training = db.get(predictions.TrainingRun, model.training_run_id)
                dataset = db.get(predictions.Dataset, training.dataset_id) if training else None
                if training is None or dataset is None:
                    return function(db, region_id)
                # A promotion, dataset change, region timezone edit or hour change
                # invalidates the response cache. No prediction POST is cached.
                key = (
                    str(region_id), str(model.id), model.artifact_uri, dataset.sha256,
                    region.timezone, json.dumps(training.configuration_json or {}, sort_keys=True),
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H"),
                )
                cached = self.dashboards.get(key)
                if cached and time.monotonic() - cached[0] < self.dashboard_ttl:
                    self.dashboards.move_to_end(key)
                    return copy.deepcopy(cached[1])
                result = function(db, region_id)
                # Remove older entries for this region and retain at most two.
                for old in list(self.dashboards):
                    if old[0] == key[0]:
                        del self.dashboards[old]
                self.dashboards[key] = (time.monotonic(), copy.deepcopy(result))
                while len(self.dashboards) > 2:
                    self.dashboards.popitem(last=False)
                return result
        return wrapped


class PredictionGate:
    """Queue a bounded number of prediction requests before DB dependencies.

    Installed inside existing CORS middleware, so busy replies retain CORS.
    Health/login/OPTIONS requests bypass the gate; each pod remains responsive.
    """
    def __init__(self, app, *, capacity=9, timeout=60):
        self.app = app
        self.capacity = capacity  # one active request plus eight waiting
        self.timeout = timeout
        self.count = 0
        self.semaphore = asyncio.Semaphore(1)

    async def busy(self, scope, receive, send):
        from starlette.responses import JSONResponse
        response = JSONResponse({"error": {
            "code": "prediction_busy", "message": "Prediction service is busy. Please try again shortly.",
            "details": {},
        }}, status_code=503, headers={"Retry-After": "2"})
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("GET", "POST") or not PROTECTED.fullmatch(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        # No await between this check and increment: atomic on the event loop.
        if self.count >= self.capacity:
            await self.busy(scope, receive, send)
            return
        self.count += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(self.semaphore.acquire(), timeout=self.timeout)
                acquired = True
            except asyncio.TimeoutError:
                await self.busy(scope, receive, send)
                return
            await self.app(scope, receive, send)
        finally:
            if acquired:
                self.semaphore.release()
            self.count -= 1

def install_neural_restore(predictions, runtime):
    if getattr(predictions, "_rke2_neural_restore", False):
        raise RuntimeError("Neural restore is already installed")
    for name in ("_load_keras_model_from_path", "_load_neural_preprocessors"):
        original = getattr(predictions, name)

        def wrap(loader):
            @functools.wraps(loader)
            def restored(filename):
                path = predictions._resolve_model_path(filename)
                runtime.ensure_file(path)
                # The verified base gate serializes prediction work; use its
                # same lock for restoration/loading without changing math.
                with runtime.lock:
                    return loader(path)
            return restored

        setattr(predictions, name, wrap(original))
    predictions._rke2_neural_restore = True


def build_app():
    from backend.app.services import predictions
    runtime = PredictionRuntime(predictions.ApplicationError, predictions.joblib.load)
    predictions._load_model = lambda version: runtime.load_model(predictions._resolve_model_path(version.artifact_uri))
    predictions.get_forecast_dashboard = runtime.dashboard(predictions, predictions.get_forecast_dashboard)
    for name in ("predict_for_region", "get_prediction_window", "_predict_tree_lag", "_predict_value"):
        setattr(predictions, name, runtime.serialized(getattr(predictions, name)))
    install_neural_restore(predictions, runtime)
    from backend.app.main import app
    from starlette.middleware import Middleware
    app.user_middleware.append(Middleware(PredictionGate))
    app.state.prediction_runtime = runtime
    app.state.training_execution_disabled = True
    app.state.neural_artifact_restore = True
    return app
