from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.app.api.router import api_router
from backend.app.core.config import get_settings
from backend.app.core.errors import register_exception_handlers
from backend.app.core.middleware import register_operational_middleware
from backend.app.db.session import SessionLocal
from backend.app.legacy.router import router as legacy_router
from backend.app.services.users import ensure_bootstrap_admin


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(title=settings.app_name, version="0.2.0")
    register_operational_middleware(application)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(application)
    application.include_router(api_router, prefix=settings.api_v1_prefix)
    application.include_router(legacy_router)
    application.mount(
        "/web",
        StaticFiles(directory=PROJECT_ROOT / "docs"),
        name="web",
    )

    @application.get("/", include_in_schema=False)
    def root():
        return FileResponse(PROJECT_ROOT / "docs" / "index.html")

    @application.on_event("startup")
    def bootstrap_admin_user() -> None:
        with SessionLocal() as session:
            ensure_bootstrap_admin(
                session,
                settings.bootstrap_admin_email,
                settings.bootstrap_admin_password,
            )

    return application


app = create_app()
