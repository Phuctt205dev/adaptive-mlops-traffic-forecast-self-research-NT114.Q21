# API Design

Base path: `/api/v1`

## Conventions

- JSON request and response bodies use snake_case.
- IDs are UUID strings.
- List endpoints support `page`, `page_size`, sorting, and relevant filters.
- Errors use a stable code and human-readable message.

```json
{
  "error": {
    "code": "dataset_validation_failed",
    "message": "The uploaded dataset contains duplicate timestamps.",
    "details": {}
  }
}
```

## Public authentication

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/auth/register` | Register a user account |
| POST | `/auth/login` | Issue access and refresh tokens |
| POST | `/auth/refresh` | Rotate access token |

## Authenticated user

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/users/me` | Current profile |
| GET | `/regions` | Regions with active models |
| POST | `/predictions` | Create prediction |
| GET | `/predictions/me` | Own prediction history |

## Admin

| Method | Path | Purpose |
| --- | --- | --- |
| GET/PATCH | `/admin/users`, `/admin/users/{id}` | Manage users |
| POST/GET/PATCH | `/admin/regions`, `/admin/regions/{id}` | Manage regions |
| POST | `/admin/regions/{id}/datasets` | Upload CSV |
| GET | `/admin/regions/{id}/datasets` | Dataset versions |
| GET | `/admin/datasets/{id}` | Dataset quality and state |
| POST | `/admin/datasets/{id}/training-runs` | Trigger training |
| GET | `/admin/training-runs/{id}` | Training status and results |
| POST | `/admin/training-runs/{id}/cancel` | Request cancellation |
| GET | `/admin/regions/{id}/model-versions` | Compare models |
| POST | `/admin/model-versions/{id}/activate` | Activate or rollback model |
| GET | `/admin/dashboard` | Dashboard aggregates |

## Authorization rules

- Registration always creates `user`; clients cannot submit a role.
- User endpoints require an active authenticated account.
- Admin endpoints require an active account with role `admin`.
- Dataset, run, and model operations verify region ownership consistency.
- The API never trusts a client-supplied artifact URI, MLflow run ID, metric,
  or model status.

## Upload behavior

`POST /admin/regions/{id}/datasets` uses `multipart/form-data`. The response is
`202 Accepted` after the raw file and metadata are durable. Validation runs
asynchronously and its state is read from `GET /admin/datasets/{id}`.
