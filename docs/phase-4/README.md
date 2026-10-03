# Phase 4 - Region and Dataset Management

## Implemented

- Authenticated user endpoint for active region selection.
- Admin CSV upload endpoint per region.
- Dataset metadata persistence in PostgreSQL.
- Raw CSV and quality report storage in MinIO bucket `traffic-datasets`.
- Contract validation for required columns, row count, timestamps, numeric
  ranges, duplicate uploads, and upload size.
- Admin dataset list/detail endpoints.

## API endpoints

```text
GET   /api/v1/regions
POST  /api/v1/admin/regions/{region_id}/datasets
GET   /api/v1/admin/regions/{region_id}/datasets
GET   /api/v1/admin/datasets/{dataset_id}
```

Admin endpoints require:

```text
Authorization: Bearer <admin_access_token>
```

## Dataset upload behavior

The upload endpoint accepts `multipart/form-data` with a `file` field.

Validation is synchronous in Phase 4 so the API can return a deterministic
status while the project is still local:

- `valid`: raw CSV and quality report are stored, metadata is ready for
  later training phases.
- `validation_failed`: raw CSV is still stored for audit, and the validation
  error is recorded in the dataset row and report.

Phase 7 can move long-running validation and training orchestration into
Airflow.

## PowerShell smoke test

```powershell
$login = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/auth/login" `
  -ContentType "application/json" `
  -Body '{"email":"admin@example.com","password":"admin123456"}'

$headers = @{ Authorization = "Bearer $($login.access_token)" }

$regions = Invoke-RestMethod `
  -Uri "http://localhost:8000/api/v1/admin/regions" `
  -Headers $headers

$regionId = $regions.items[0].id

curl.exe -X POST "http://localhost:8000/api/v1/admin/regions/$regionId/datasets" `
  -H "Authorization: Bearer $($login.access_token)" `
  -F "file=@data/TrafficVolumeData.csv;type=text/csv"

Invoke-RestMethod `
  -Uri "http://localhost:8000/api/v1/admin/regions/$regionId/datasets" `
  -Headers $headers
```
