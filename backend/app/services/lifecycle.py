"""Serving readiness and request draining; no deployment lock on user traffic."""
import threading


class ServingState:
    def __init__(self):
        self.warmed = False
        self.draining = False
        self.inflight = 0
        self.lock = threading.Lock()


class InflightMiddleware:
    def __init__(self, app, *, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        tracked = scope['type'] == 'http' and '/health/' not in path and '/internal/lifecycle/' not in path
        if tracked:
            with self.state.lock:
                self.state.inflight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            if tracked:
                with self.state.lock:
                    self.state.inflight -= 1


def warm_active_models(app):
    from sqlalchemy import select
    from backend.app.db.models import Region, ModelVersion, TrainingRun
    from backend.app.db.session import SessionLocal
    from backend.app.services import predictions
    with SessionLocal() as db:
        rows = db.scalars(select(Region).where(Region.is_active.is_(True), Region.active_model_version_id.is_not(None))).all()
        for region in rows:
            model = db.get(ModelVersion, region.active_model_version_id)
            if model is None:
                raise RuntimeError('An active model reference is missing')
            run = db.get(TrainingRun, model.training_run_id)
            configuration = (run.configuration_json or {}) if run else {}
            if configuration.get('model_family') == 'neural_sequence':
                preprocessor = configuration.get('preprocessor_file')
                if not preprocessor:
                    raise RuntimeError('An active neural preprocessor is missing')
                predictions._load_neural_preprocessors(preprocessor)
                predictions._load_keras_model(model)
            else:
                predictions._load_model(model)
    app.state.serving_lifecycle.warmed = True


def install(app):
    import hmac
    from fastapi import Header, HTTPException
    from fastapi.responses import JSONResponse
    from backend.app.core.config import get_settings
    state = ServingState()
    app.state.serving_lifecycle = state
    app.add_middleware(InflightMiddleware, state=state)

    @app.on_event('startup')
    def warm():
        warm_active_models(app)

    @app.get('/api/v1/health/serving-ready', include_in_schema=False)
    def ready():
        if not state.warmed or state.draining:
            return JSONResponse({'status': 'not_ready'}, status_code=503)
        from backend.app.api.routes.health import readiness
        return readiness()

    def authorize(token):
        if not token or not hmac.compare_digest(token, get_settings().internal_training_token):
            raise HTTPException(status_code=403, detail='Invalid lifecycle token')

    @app.post('/api/v1/internal/lifecycle/drain', include_in_schema=False)
    def drain(x_internal_token: str | None = Header(default=None)):
        authorize(x_internal_token)
        state.draining = True
        return {'draining': True, 'inflight': state.inflight}

    @app.get('/api/v1/internal/lifecycle/status', include_in_schema=False)
    def status(x_internal_token: str | None = Header(default=None)):
        authorize(x_internal_token)
        return {'draining': state.draining, 'warmed': state.warmed, 'inflight': state.inflight}

    @app.post('/api/v1/internal/lifecycle/resume', include_in_schema=False)
    def resume(x_internal_token: str | None = Header(default=None)):
        authorize(x_internal_token)
        state.draining = False
        return {'draining': False, 'warmed': state.warmed}


def prestop():
    """Mark unready, allow endpoint propagation, then wait for active requests."""
    import json
    import os
    import time
    from urllib.request import Request, build_opener, ProxyHandler
    opener = build_opener(ProxyHandler({}))
    headers = {'X-Internal-Token': os.environ['INTERNAL_TRAINING_TOKEN']}
    request = Request('http://127.0.0.1:8000/api/v1/internal/lifecycle/drain', data=b'', headers=headers, method='POST')
    with opener.open(request, timeout=10) as response:
        json.load(response)
    time.sleep(10)
    deadline = time.monotonic() + 840
    while time.monotonic() < deadline:
        request = Request('http://127.0.0.1:8000/api/v1/internal/lifecycle/status', headers=headers)
        with opener.open(request, timeout=10) as response:
            value = json.load(response)
        if value['inflight'] == 0:
            return
        time.sleep(.5)
    raise RuntimeError('Request drain exceeded its reviewed budget')


if __name__ == '__main__':
    prestop()
