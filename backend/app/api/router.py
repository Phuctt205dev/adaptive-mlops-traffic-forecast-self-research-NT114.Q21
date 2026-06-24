from fastapi import APIRouter

from backend.app.api.routes import (
    auth,
    datasets,
    drift,
    health,
    internal_drift,
    internal_training,
    model_versions,
    predictions,
    public_regions,
    regions,
    system,
    users,
)


api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(
    internal_training.router,
    prefix="/internal",
    tags=["internal"],
)
api_router.include_router(
    internal_drift.router,
    prefix="/internal",
    tags=["internal"],
)
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(public_regions.router, tags=["regions"])
api_router.include_router(predictions.router, tags=["predictions"])
api_router.include_router(system.router, prefix="/admin", tags=["system"])
api_router.include_router(users.router, prefix="/admin/users", tags=["users"])
api_router.include_router(regions.router, prefix="/admin/regions", tags=["regions"])
api_router.include_router(datasets.router, prefix="/admin", tags=["datasets"])
api_router.include_router(model_versions.router, prefix="/admin", tags=["models"])
api_router.include_router(drift.router, prefix="/admin", tags=["drift"])
