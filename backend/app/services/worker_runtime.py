"""Kaggle-only worker HTTP contract, without legacy fit or serving imports."""
import hmac,importlib,json,os,re,threading,uuid
import time
from pathlib import Path
from types import SimpleNamespace

NODES={'tree':'mlops-node-02','recurrent':'mlops-node-03'}
CONTROL=re.compile(r"^/api/v1/internal/training-runs/([^/]+)/control/(?:(tree|lstm|gru)/(start|status)|(stop|finalize))$")

class WorkerGate:
    def __init__(self,app,*,role,token):
        self.app,self.role,self.token=app,role,token.encode()

    async def reject(self,send,status,code):
        raw=json.dumps({'error':{'code':code,'message':'Worker control request was rejected.','details':{}}}).encode()
        await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json')]})
        await send({'type':'http.response.body','body':raw})

    async def __call__(self,scope,receive,send):
        path=scope.get('path','')
        if scope['type']!='http' or not path.startswith('/api/v1/internal/training-runs'):
            return await self.app(scope,receive,send)
        values=[v for k,v in scope.get('headers',[]) if k.lower()==b'x-internal-token']
        if len(values)!=1 or not hmac.compare_digest(values[0],self.token):
            return await self.reject(send,403,'internal_token_invalid')
        match=CONTROL.fullmatch(path)
        if not match or scope.get('method')!='POST':
            return await self.reject(send,409,'worker_control_endpoint_required')
        try:
            if str(uuid.UUID(match[1]))!=match[1]:raise ValueError()
        except ValueError:
            return await self.reject(send,422,'training_run_id_invalid')
        if ((match[2]=='tree' or match[4]=='finalize') and self.role!='tree') or (match[2] in {'lstm','gru'} and self.role!='recurrent'):
            return await self.reject(send,409,'worker_role_mismatch')
        return await self.app(scope,receive,send)

def add_control(app, base, operation_lock, project, finalizer):
    from fastapi import APIRouter, Body, Depends
    from backend.app.db.models import ModelVersion, TrainingRun, TrainingRunStatus
    from backend.app.db.session import SessionLocal, engine
    from backend.app.services import training_execution as state
    from backend.app.services.training_control import WorkerController
    training = importlib.import_module("backend.app.services.worker_context")
    settings = training.get_settings()
    bridge = training._rke2_result_bridge
    role = os.environ["TRAFFIC_TRAINING_ROLE"]
    identity = {"role": role, "node": base.NODES[role], "pod": os.environ["POD_NAME"],
                "runtime_revision": base.REVISION}
    from src.cloud_training.kaggle_transport import SDKTransport
    from backend.app.services.region_cleanup import database_barrier
    status_state = app.state.operation_status
    controller = WorkerController(SessionLocal, training, bridge, role=role, identity=identity,
        root="/app/models/distributed-training/v1", project=project, username=settings.kaggle_username,
        transport=SDKTransport(settings.kaggle_username, python=os.environ.get("KAGGLE_PYTHON"), repository=Path('/app')),
        operation_lock=operation_lock, barrier_factory=lambda: database_barrier(engine), status_state=status_state,
        producer_profile=json.loads(Path('/app/role-release.json').read_text(encoding='utf-8'))['producer_profile'])
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
        from backend.app.services.deployment_fence import execution_admission
        with execution_admission(engine):
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
                if record.backend != 'kaggle':
                    raise training.ApplicationError('local_training_removed', 'This worker finalizes Kaggle training only.', 422)
                state.publication_gate(db, run_id)
                expected = {branch: item.get("receipt") for branch, item in record.branches_json.items()}
                if payload["artifact_receipts"] != expected:
                    raise training.ApplicationError("training_receipt_mismatch", "Finalize receipts differ from the accepted branches.", 409)
                try:
                    finalization_started = time.monotonic()
                    result = finalizer(training_run_id=run_id, payload=payload, _token=None, db=db)
                    finalization_seconds = time.monotonic() - finalization_started
                    try:
                        state.record_finalization_timing(db, run_id, finalization_seconds)
                    except Exception:
                        db.rollback()  # Missing telemetry cannot undo confirmed publication.
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


def build_app(project):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from backend.app.core.errors import register_exception_handlers
    from backend.app.db.session import engine
    from backend.app.services import worker_context as training,model_registry as registry
    from backend.app.services.storage import get_s3_client
    from backend.app.services.training_artifacts import BranchArtifacts
    from backend.app.services.training_result_bridge import TrainingResultBridge
    role=os.environ['TRAFFIC_TRAINING_ROLE']
    if role not in NODES or os.environ.get('POD_NODE_NAME')!=NODES[role] or not os.environ.get('POD_NAME'):
        raise RuntimeError('Worker placement does not match its reviewed role')
    settings=training.get_settings()
    token=os.environ.get('INTERNAL_TRAINING_TOKEN','')
    if not token or not hmac.compare_digest(token,settings.internal_training_token):
        raise RuntimeError('Worker token configuration is invalid')
    for path in ['/app/models','/app/results/training-receive','/app/models/distributed-serving/v1']:
        Path(path).mkdir(parents=True,exist_ok=True)
    bridge=TrainingResultBridge(BranchArtifacts(get_s3_client(),os.environ['TRAFFIC_API_RUNTIME_BUCKET']),
        artifact_root='/app/models',receive_root='/app/results/training-receive',model_root='/app/models',serving_root='/app/models/distributed-serving/v1')
    bridge.install(training,registry)
    def finalize(training_run_id,payload,_token,db):
        run,_,_=training._get_training_context(db,training_run_id)
        with bridge.finalize_scope(run,payload['artifact_receipts']):
            return training.finalize_parallel_training_run(training_run_id,_token,db)
    app=FastAPI(title='Kaggle '+role+' controller',docs_url=None,redoc_url=None)
    register_exception_handlers(app)
    app.state.operation_status={'busy':False,'operation_count':0}
    app.add_middleware(WorkerGate,role=role,token=token)
    base=SimpleNamespace(NODES=NODES,REVISION='branch-execution-v1')
    add_control(app,base,threading.Lock(),Path(project),finalize)
    @app.get('/health/live')
    def live():
        return {'status':'ok','role':role,'runtime_revision':'branch-execution-v1'}
    @app.get('/health/ready')
    def ready():
        from sqlalchemy import text
        try:
            with engine.connect() as c:c.execute(text('SELECT 1'))
            return {'status':'ready','role':role}
        except Exception:
            return JSONResponse({'status':'not_ready'},status_code=503)
    @app.get('/health/integration')
    def integration():
        return {'status':'ok','role':role,'node':NODES[role],'pod':os.environ['POD_NAME'],
                'runtime_revision':'branch-execution-v1','control_revision':'cloud-training-v1',
                'artifact_bridge_installed':True,'receipt_finalize':True,'run_artifact_isolation':True,
                'training_enabled':True,'max_active_operations':1,**app.state.operation_status}
    return app
