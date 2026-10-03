# Phase 9: Connect Frontend To API

This phase connects the GitHub Pages frontend to the public API endpoint created
from the EKS deployment.

## Important HTTPS Rule

GitHub Pages is served over HTTPS. A browser will block requests from GitHub
Pages to an `http://` API endpoint.

Use one of these options:

- Recommended: complete Phase 10 first, then use an HTTPS API URL.
- Temporary only: deploy with HTTP for testing, but the public website will not
  work reliably in normal browsers.

## What This Phase Adds

- Manual workflow inputs for the GitHub Pages deployment.
- Script to set GitHub repository variables and trigger frontend redeploy.
- Smoke test for the public frontend and API endpoint.

## Files

```text
.github/workflows/deploy-frontend-pages.yml
scripts/deploy/connect_frontend_api.ps1
scripts/deploy/smoke_frontend_pages.ps1
```

## Prerequisites

Complete these first:

- Phase 2: GitHub Pages frontend deployment
- Phase 7: API on EKS
- Phase 8: Airflow + MLflow if you need training from the website

For real browser use, also complete:

- Phase 10: Domain + HTTPS

## Connect GitHub Pages To The API

When you have an HTTPS API base URL:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/connect_frontend_api.ps1 `
  -ApiBaseUrl "https://api.example.com/api/v1"
```

The script:

- sets `VITE_API_BASE_URL`
- sets `VITE_BASE_PATH`
- triggers the GitHub Pages deployment workflow

If you must test with a temporary HTTP ALB URL:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/connect_frontend_api.ps1 `
  -ApiBaseUrl "http://<alb-dns-name>/api/v1" `
  -AllowHttpApi
```

This is useful only for checking the workflow and generated frontend config.
GitHub Pages to HTTP API calls are expected to fail in normal browsers.

## Smoke Test

After GitHub Actions finishes:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/smoke_frontend_pages.ps1 `
  -FrontendUrl "https://phuctt205dev.github.io/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/" `
  -ApiBaseUrl "https://api.example.com/api/v1" `
  -AdminPassword "<bootstrap-admin-password>"
```

Expected checks:

- GitHub Pages returns the React app shell.
- API liveness works.
- API readiness works.
- Optional admin login works.

## Manual GitHub Settings

If you do not want to use the script, set these repository variables manually:

```text
VITE_API_BASE_URL=https://api.example.com/api/v1
VITE_BASE_PATH=/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/
```

Then run:

```text
Actions -> Deploy Frontend To GitHub Pages -> Run workflow
```

## Phase 9 Done Criteria

- GitHub repository variables point to the intended API URL and base path.
- GitHub Pages workflow finishes successfully.
- The deployed frontend loads from GitHub Pages.
- API health checks pass from the public API URL.
- Login from the GitHub Pages website works after HTTPS is available.
