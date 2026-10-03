from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from backend.app.core.config import get_settings
from backend.app.db.session import engine


router = APIRouter()


def _database_health() -> dict:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "healthy"}
    except Exception:
        return {"status": "unhealthy"}


def _http_health(url: str, timeout: float) -> dict:
    try:
        with urlopen(url, timeout=timeout) as response:
            if 200 <= response.status < 300:
                return {"status": "healthy"}
            return {"status": "unhealthy", "detail": f"HTTP {response.status}"}
    except Exception:
        return {"status": "unhealthy"}


@router.get("/health/live")
def liveness():
    return {"status": "ok"}


@router.get("/health/ready")
def readiness():
    settings = get_settings()
    with ThreadPoolExecutor(max_workers=3) as executor:
        database = executor.submit(_database_health)
        minio = executor.submit(
            _http_health,
            settings.minio_health_url,
            settings.external_health_timeout_seconds,
        )
        mlflow = executor.submit(
            _http_health,
            settings.mlflow_health_url,
            settings.external_health_timeout_seconds,
        )
    services = {
        "database": database.result(),
        "minio": minio.result(),
        "mlflow": mlflow.result(),
    }
    status = (
        "ready"
        if all(service["status"] == "healthy" for service in services.values())
        else "not_ready"
    )
    content = {"status": status, "services": services}
    if status == "not_ready":
        return JSONResponse(status_code=503, content=content)
    return content
