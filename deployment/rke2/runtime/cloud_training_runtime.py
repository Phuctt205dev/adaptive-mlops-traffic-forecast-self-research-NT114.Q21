"""Additive cloud/control entrypoint; old image files and guards stay intact."""
import functools
import hashlib
import hmac
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import sys
import threading
import uuid

ROOT = Path("/opt/traffic-cloud")
CONTROL = re.compile(r"^/api/v1/internal/training-runs/([^/]+)/control/(?:(tree|lstm|gru)/(start|status)|(stop|finalize))$")


class Overlay(importlib.abc.MetaPathFinder):
    def __init__(self, sources):
        self.sources = sources

    def find_spec(self, fullname, path=None, target=None):
        source = self.sources.get(fullname)
        if source:
            return importlib.util.spec_from_file_location(fullname, source,
                submodule_search_locations=[str(source.parent)] if source.name == "__init__.py" else None)


class MissingPackages(importlib.abc.MetaPathFinder):
    """Last-resort parents for reviewed modules absent from the base image."""
    def __init__(self, document, root):
        self.parents = {".".join(name.split(".")[:index])
                        for name in document["modules"] for index in range(1, len(name.split(".")))}
        self.files, self.root = document["files"], root

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.parents:
            return None
        directory = self.root / "project" / fullname.replace(".", "/")
        initializer = directory / "__init__.py"
        if initializer.relative_to(self.root).as_posix() in self.files:
            return importlib.util.spec_from_file_location(fullname, initializer,
                                                         submodule_search_locations=[str(directory)])
        spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
        spec.submodule_search_locations = [str(directory)]
        return spec


def verify(root=ROOT):
    document = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    content = {key: value for key, value in document.items() if key != "bundle_id"}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if document.get("revision") != "cloud-training-v1" or document.get("bundle_id") != digest:
        raise RuntimeError("Unexpected cloud training bundle identity.")
    for name, expected in document["files"].items():
        path = (root / name).resolve(strict=True)
        # Projected ConfigMaps use Kubernetes-owned symlinks. Resolve inside
        # this immutable volume and verify hashes; artifact paths stay stricter.
        if (not path.is_relative_to(root.resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise RuntimeError("Cloud training source differs from the reviewed bundle.")
    return document


def install(document, root=ROOT):
    sources = {name: root / path for name, path in document["modules"].items()}
    if any(name in sys.modules for name in sources):
        raise RuntimeError("Cloud overlay must be installed before application modules.")
    sys.meta_path.insert(0, Overlay(sources))
    # Existing outer/image package loaders retain priority. New parent
    # packages still need a spec before Python can ask for their leaf modules.
    sys.meta_path.append(MissingPackages(document, root))


def check_imports(root):
    """Build-time import graph check; no app lifespan, worker, or DB query."""
    document = verify(root)
    sys.path.insert(0, "/app")
    install(document, root)
    importlib.import_module("backend.app.services.training_control")
    importlib.import_module("backend.app.api.routes.training_monitor")
    print(json.dumps({"application_imports_ready": True, "bundle_id": document["bundle_id"]}), flush=True)


def build_serving_app(shap):
    """Compose verified serving guards with features already in tracked API."""
    manifest = shap.verify_bundle(shap.ROOT, "api")
    shap.install_modules(manifest, "api", shap.ROOT)
    quality_root = Path("/opt/traffic-quality")
    spec = importlib.util.spec_from_file_location("release_quality_contract", quality_root / "model_quality_runtime.py")
    quality = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(quality)
    quality.verify_bundle(quality_root, Path("/opt/traffic-runtime"), Path("/app"))
    sys.path.insert(0, "/opt/traffic-runtime")
    serving = importlib.import_module("prediction_serving_runtime")
    app = serving.build_app()
    if not getattr(app.state, "training_execution_disabled", False) or not hasattr(app.state, "prediction_runtime"):
        raise RuntimeError("The original serving guards are required.")
    expected = {quality.QUALITY_PATH, "/api/v1/admin/regions/{region_id}/feature-importance"}
    if not expected.issubset(app.openapi()["paths"]):
        raise RuntimeError("The tracked API must provide the reviewed quality and feature routes.")

    @app.get(quality.HEALTH_PATH, include_in_schema=False)
    def quality_health():
        return {"status": "ok", "revision": quality.REVISION, "quality_window": "recent",
                "reference_source": "dataset_traffic_volume", "supported_hours": [24, 168, 336],
                "default_hours": 168, "refresh_policy": "hour_boundary", "training_execution_disabled": True}

    @app.get("/api/v1/health/shap", include_in_schema=False)
    def shap_health():
        return {"status": "ok", "revision": shap.REVISION, "bundle_id": manifest["bundle_id"],
                "mode": "api", "profile": "trees-first", "feature_importance_ready": True}

    app.state.model_quality_revision = quality.REVISION
    app.openapi_schema = None
    return app


def gate_class(original, operation_lock):
    class ControlGate(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.lock = operation_lock

        async def __call__(self, scope, receive, send):
            path = scope.get("path", "")
            if scope.get("type") != "http" or "/control/" not in path:
                return await super().__call__(scope, receive, send)
            tokens = [value for key, value in scope.get("headers", []) if key.lower() == b"x-internal-token"]
            if len(tokens) != 1 or not hmac.compare_digest(tokens[0], self.token):
                return await self.reject(send, 403, "internal_token_invalid")
            match = CONTROL.fullmatch(path)
            if not match or scope.get("method") != "POST":
                return await self.reject(send, 409, "worker_control_endpoint_required")
            try:
                if str(uuid.UUID(match[1])) != match[1]:
                    raise ValueError()
            except ValueError:
                return await self.reject(send, 422, "training_run_id_invalid")
            branch = match[2]
            if (branch == "tree" or match[4] == "finalize") and self.role != "tree":
                return await self.reject(send, 409, "worker_role_mismatch")
            if branch in {"lstm", "gru"} and self.role != "recurrent":
                return await self.reject(send, 409, "worker_role_mismatch")
            # Status and stop remain available while the shared operation lease
            # is held by a background fit. start acquires that lease itself.
            return await self.app(scope, receive, send)
    return ControlGate


def install_result_contract():
    bridge = importlib.import_module("training_result_bridge")
    original = bridge.check_result
    if getattr(original, "_cloud_contract", False):
        return
    bridge.FILE_FIELDS = {*bridge.FILE_FIELDS, "compatibility_file"}

    def check(run, branch, result):
        config = run.configuration_json or {}
        if config.get("_execution_protocol") == 2 and branch == "tree" and result.get("status") == "skipped":
            selected = config.get("selected_models") or []
            if (result.get("branch") != "tree" or any(name.endswith("_lag") for name in selected)
                    or result.get("candidates") != [] or result.get("production_candidate")):
                raise bridge.ArtifactError("Required tree branch cannot be skipped.")
            return
        return original(run, branch, result)

    check._cloud_contract = True
    bridge.check_result = check


def intercept_legacy_routes():
    integrated = importlib.import_module("training_worker_integrated")
    configure = integrated.configure_routes
    captured = {}
    def controlled_routes(training, bridge):
        router = configure(training, bridge)
        for route in router.routes:
            if route.path.endswith("/finalize"):
                captured["finalizer"] = route.endpoint
            if "/execute/" in route.path:
                original = route.endpoint
                @functools.wraps(original)
                def guard(*args, _endpoint=original, **kwargs):
                    run, _, _ = training._get_training_context(kwargs["db"], kwargs["training_run_id"])
                    if (run.configuration_json or {}).get("_execution_protocol") == 2:
                        raise training.ApplicationError("training_control_required", "Use controlled execution for this run.", 409)
                    return _endpoint(*args, **kwargs)
                route.endpoint = guard
        return router
    integrated.configure_routes = controlled_routes
    return captured


def add_control(app, base, operation_lock, project, finalizer):
    from fastapi import APIRouter, Body, Depends
    from backend.app.db.models import ModelVersion, TrainingRun, TrainingRunStatus
    from backend.app.db.session import SessionLocal, engine
    from backend.app.services import training_execution as state
    from backend.app.services.training_control import WorkerController
    training = importlib.import_module("backend.app.api.routes.internal_training")
    settings = training.get_settings()
    bridge = training._rke2_result_bridge
    role = os.environ["TRAFFIC_TRAINING_ROLE"]
    identity = {"role": role, "node": base.NODES[role], "pod": os.environ["POD_NAME"],
                "runtime_revision": base.REVISION}
    from src.cloud_training.kaggle_transport import SDKTransport
    from backend.app.services.region_cleanup import database_barrier
    status_state = next(item.kwargs["state"] for item in app.user_middleware if item.cls is base.ExecutionGate)
    controller = WorkerController(SessionLocal, training, bridge, role=role, identity=identity,
        root="/app/models/distributed-training/v1", project=project, username=settings.kaggle_username,
        transport=SDKTransport(settings.kaggle_username, python=os.environ.get("KAGGLE_PYTHON"), repository=project),
        operation_lock=operation_lock, barrier_factory=lambda: database_barrier(engine), status_state=status_state)
    original_register = training.register_training_result
    def tracked_register(db, region_id, dataset_id, info, training_run_id=None):
        run = db.get(TrainingRun, training_run_id)
        if run and (run.configuration_json or {}).get("_execution_protocol") == 2:
            from backend.app.services.training_tracking import TrainingTracker
            info = TrainingTracker().publish(run, info)
        return original_register(db, region_id, dataset_id, info, training_run_id=training_run_id)
    training.register_training_result = tracked_register
    if finalizer is None:
        raise RuntimeError("The guarded receipt finalizer was not found.")
    router = APIRouter(dependencies=[Depends(training.require_internal_token)])

    @router.post("/training-runs/{run_id}/control/{branch}/start")
    def start(run_id: uuid.UUID, branch: str):
        return controller.start(run_id, branch)

    @router.post("/training-runs/{run_id}/control/{branch}/status")
    def status(run_id: uuid.UUID, branch: str, payload: dict = Body(...)):
        if set(payload) != {"attempt_id", "after_sequence"}:
            raise training.ApplicationError("training_poll_invalid", "Invalid polling payload.", 422)
        return controller.status(run_id, branch, payload["attempt_id"], payload["after_sequence"])

    @router.post("/training-runs/{run_id}/control/stop")
    def stop(run_id: uuid.UUID):
        with SessionLocal() as db:
            state.request_stop(db, run_id)
        return {"stop_requested": True, "stop_confirmed": False,
                "execution_worker": {**identity, "control_revision": "cloud-training-v1"}}

    @router.post("/training-runs/{run_id}/control/finalize")
    def finalize(run_id: uuid.UUID, payload: dict = Body(...)):
        if role != "tree" or set(payload) != {"artifact_receipts"}:
            raise training.ApplicationError("training_receipts_required", "Finalize requires the tree worker and branch receipts.", 409)
        if not operation_lock.acquire(blocking=False):
            raise training.ApplicationError("training_worker_busy", "Worker is still processing an operation.", 503)
        status_state["busy"] = True
        status_state["operation_count"] += 1
        try:
            with SessionLocal() as db:
                run = db.get(TrainingRun, run_id)
                if not run or (run.configuration_json or {}).get("_execution_protocol") != 2:
                    raise training.ApplicationError("training_protocol_mismatch", "A controlled run is required.", 409)
                record = state.execution(db, run_id, lock=True)
                if record.state == "completed":
                    version = db.get(ModelVersion, run.recommended_model_version_id)
                    return {"training_run_id": str(run.id), "training_run_status": "completed",
                            "model_version_id": str(version.id), "model_version": version.version,
                            "execution_worker": {**identity, "control_revision": "cloud-training-v1"}}
                state.publication_gate(db, run_id)
                expected = {branch: item.get("receipt") for branch, item in record.branches_json.items()}
                if payload["artifact_receipts"] != expected:
                    raise training.ApplicationError("training_receipt_mismatch", "Finalize receipts differ from the accepted branches.", 409)
                try:
                    result = finalizer(training_run_id=run_id, payload=payload, _token=None, db=db)
                    return {**result, "execution_worker": {**identity, "control_revision": "cloud-training-v1"}}
                except Exception:
                    db.rollback()
                    record = state.execution(db, run_id)
                    if record.state == "completed":
                        run = db.get(TrainingRun, run_id)
                        run.status = TrainingRunStatus.COMPLETED
                        db.commit()
                    else:
                        state.request_stop(db, run_id, "publication_failed")
                    raise training.ApplicationError("training_publication_failed", "Final publication could not be confirmed.", 503) from None
        finally:
            status_state["busy"] = False
            operation_lock.release()

    app.include_router(router, prefix="/api/v1/internal")

    @app.get("/health/cloud-training")
    def health():
        return {"status": "ok", "revision": "cloud-training-v1", "cloud_enabled": settings.cloud_training_enabled,
                "bundle_id": getattr(app.state, "cloud_bundle_id", None), "role": role,
                "neural_shap_supported": True,
                "background_operations": sum(bool(h["lease_owned"]) for h in controller.handles.values())}

    app.router.add_event_handler("startup", controller.boot)
    app.router.add_event_handler("shutdown", controller.shutdown.set)
    app.state.training_controller = controller


def install_neural_shap_dependencies(root=Path("/opt/traffic-shap"), target=Path("/opt/traffic-shap-deps")):
    # The startup checker supplies temporary copies of these exact immutable
    # resources. Production always uses the reviewed mounted paths above.
    spec = importlib.util.spec_from_file_location("neural_shap_dependencies", root / "shap_system_runtime.py")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    manifest = runtime.verify_bundle(root, "worker")
    marker = json.loads((target / "installed.json").read_text(encoding="utf-8"))
    if marker != {"bundle_id": manifest["bundle_id"], "wheels": manifest["wheels"]}:
        raise RuntimeError("Neural SHAP dependencies differ from the reviewed wheels.")
    sys.path.insert(0, str(target))


def build_app(root=ROOT, *, dependency_root=None, dependency_target=None):
    document = verify(root)
    mode = os.environ.get("TRAFFIC_CLOUD_MODE")
    if mode not in {"worker", "api"}:
        raise RuntimeError("Explicit cloud worker/api mode is required.")
    if mode == "worker" and os.environ.get("TRAFFIC_TRAINING_ROLE") == "recurrent":
        # Reuse the immutable, reviewed SHAP wheels without changing the base
        # image or enabling the older trees-first module overlay for this role.
        install_neural_shap_dependencies(**({"root": dependency_root, "target": dependency_target}
                                            if dependency_root is not None and dependency_target is not None else {}))
    project = root / "project"
    # Keep the currently deployed outer layer, including its original-file
    # hashes, import overlays and shared database barrier for active work.
    outer_path = Path("/opt/region-cleanup/region_cleanup_entrypoint.py")
    if not outer_path.is_file():
        raise RuntimeError("The reviewed outer runtime is required.")
    outer_spec = importlib.util.spec_from_file_location("cloud_outer_compat", outer_path)
    outer = importlib.util.module_from_spec(outer_spec)
    outer_spec.loader.exec_module(outer)
    outer_manifest = outer.verify()
    outer.install_sources(outer_manifest)
    sys.path.insert(0, "/app")
    sys.path.insert(0, "/opt/traffic-training")
    operation_lock = threading.Lock()
    if mode == "worker":
        base = importlib.import_module("training_worker_execution")
        base.verify_runtime("/opt/traffic-training")
        importlib.import_module("training_worker_staging").verify_sources()
        base.ExecutionGate = gate_class(base.ExecutionGate, operation_lock)
        install_result_contract()
        captured_routes = intercept_legacy_routes()
    use_shap = mode == "api" or os.environ.get("TRAFFIC_TRAINING_ROLE") == "tree"
    if use_shap:
        sys.path.insert(0, "/opt/traffic-shap")
        shap = importlib.import_module("shap_system_runtime")
        original_install = shap.install_modules
        def combined(manifest, selected_mode, selected_root=shap.ROOT):
            finder = original_install(manifest, selected_mode, selected_root)
            install(document, root)
            return finder
        shap.install_modules = combined
        app = build_serving_app(shap) if mode == "api" else shap.build_app(mode=mode)
    else:
        install(document, root)
        app = base.build_app()
    if mode == "worker":
        app.state.cloud_bundle_id = document["bundle_id"]
        add_control(app, base, operation_lock, project, captured_routes.get("finalizer"))
    else:
        monitor = importlib.import_module("backend.app.api.routes.training_monitor")
        if "/api/v1/admin/training-monitor/capabilities" not in app.openapi()["paths"]:
            app.include_router(monitor.router, prefix="/api/v1/admin", tags=["training-monitor"])
        app.openapi_schema = None
        @app.get("/api/v1/health/cloud-training", include_in_schema=False)
        def cloud_health():
            from backend.app.core.config import get_settings
            settings = get_settings()
            return {"status": "ok", "revision": "cloud-training-v1", "bundle_id": document["bundle_id"],
                    "neural_shap_supported": True,
                    "execution_control": settings.training_control_enabled, "cloud_enabled": settings.cloud_training_enabled}
    from backend.app.services.region_cleanup_runtime import install as install_outer
    role = os.environ["REGION_CLEANUP_ROLE"]
    install_outer(app, role=role, log_root="/opt/region-cleanup-airflow-logs" if role == "recurrent" else None)
    health_path = "/api/v1/health/region-cleanup" if role == "api" else "/health/region-cleanup"
    @app.get(health_path, include_in_schema=False)
    def outer_health():
        return {"status": "ok", "revision": outer.REVISION, "bundle_id": outer_manifest["bundle_id"], "role": role}
    release_file = Path("/opt/traffic-release/release.json")
    if release_file.is_file():
        release = json.loads(release_file.read_text(encoding="utf-8"))
        if release.get("cloud_bundle_id") != document["bundle_id"]:
            raise RuntimeError("Image release and mounted cloud sources differ.")
        path = "/api/v1/health/release" if mode == "api" else "/health/release"
        @app.get(path, include_in_schema=False)
        def release_health():
            return {"status": "ok", "commit": release["commit"], "release_id": release["release_id"],
                    "bundle_id": document["bundle_id"], "role": role,
                    "base_image": release["baseline"]["application_base_image"]}
    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-imports", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.check_imports:
        check_imports(args.root)
    else:
        import uvicorn
        uvicorn.run(build_app(root=args.root), host="0.0.0.0", port=8000, workers=1, access_log=False)
