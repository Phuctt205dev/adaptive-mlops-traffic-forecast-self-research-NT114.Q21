# Phase 3 - Authentication and User Management

## Implemented

- User registration with default `user` role.
- Login endpoint returning a bearer access token.
- Current-user endpoint.
- Admin-only user management endpoints.
- Admin-only protection for region management endpoints.
- Bootstrap admin account from environment variables.
- PBKDF2 password hashing and HS256 signed access tokens.

## Default local admin

The `traffic-api` service creates a local admin account on startup when these
environment variables are present:

```text
BOOTSTRAP_ADMIN_EMAIL=admin@example.com
BOOTSTRAP_ADMIN_PASSWORD=admin123456
AUTH_SECRET_KEY=local-dev-auth-secret-change-me
```

These defaults are for local development only. Change them in `.env` before any
shared or deployed environment.

## API endpoints

```text
POST  /api/v1/auth/register
POST  /api/v1/auth/login
GET   /api/v1/auth/me
GET   /api/v1/admin/users
GET   /api/v1/admin/users/{user_id}
PATCH /api/v1/admin/users/{user_id}
GET   /api/v1/admin/regions
POST  /api/v1/admin/regions
GET   /api/v1/admin/regions/{region_id}
PATCH /api/v1/admin/regions/{region_id}
```

All `/api/v1/admin/*` endpoints require:

```text
Authorization: Bearer <access_token>
```

## PowerShell smoke test

```powershell
$login = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/auth/login" `
  -ContentType "application/json" `
  -Body '{"email":"admin@example.com","password":"admin123456"}'

$headers = @{ Authorization = "Bearer $($login.access_token)" }

Invoke-RestMethod `
  -Uri "http://localhost:8000/api/v1/auth/me" `
  -Headers $headers

Invoke-RestMethod `
  -Uri "http://localhost:8000/api/v1/admin/users" `
  -Headers $headers
```
