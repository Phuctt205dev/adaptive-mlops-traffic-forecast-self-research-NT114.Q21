# Phase 2 - Backend Foundation and Database

## Implemented

- Modular FastAPI application under `backend/app`.
- Compatibility entrypoint remains `app:app`.
- Pydantic settings loaded from environment variables and optional `.env`.
- SQLAlchemy 2 session management and metadata models.
- Alembic initial migration for users, regions, datasets, training runs,
  model versions, predictions, and audit logs.
- Liveness and dependency readiness endpoints.
- Basic region CRUD service and API.
- Existing `/`, `/health`, `/model-info`, and `/predict` endpoints retained.
- A dedicated lightweight API image avoids installing TensorFlow in the web
  API container. Its dependencies are explicit in `requirements/backend.txt`.
  Training dependencies remain in the root `requirements.txt`.

## Database migration

```powershell
alembic upgrade head
alembic current
```

The `traffic-api` Compose service automatically runs `alembic upgrade head`
before starting Uvicorn.

## API endpoints

```text
GET   /api/v1/health/live
GET   /api/v1/health/ready
GET   /api/v1/admin/regions
POST  /api/v1/admin/regions
GET   /api/v1/admin/regions/{region_id}
PATCH /api/v1/admin/regions/{region_id}
```

Region admin routes were intentionally unauthenticated only during Phase 2.
Phase 3 protects them with admin token/RBAC dependencies.
