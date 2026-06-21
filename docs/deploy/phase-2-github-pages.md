# Phase 2: Frontend GitHub Pages

This phase deploys the React/Vite frontend as a static GitHub Pages site. The
backend is not deployed in this phase.

## What This Phase Adds

- `.github/workflows/deploy-frontend-pages.yml`
  - builds the app from `frontend`
  - publishes `frontend/dist` to GitHub Pages
  - reads `VITE_API_BASE_URL` and `VITE_BASE_PATH` from GitHub repository
    variables
- `frontend/public/.nojekyll`
  - keeps GitHub Pages from applying Jekyll processing to the Vite build output

## GitHub Repository Settings

In GitHub, open:

```text
Repository -> Settings -> Pages
```

Set:

```text
Source: GitHub Actions
```

Then open:

```text
Repository -> Settings -> Secrets and variables -> Actions -> Variables
```

Recommended variables:

```env
VITE_BASE_PATH=/<repo-name>/
VITE_API_BASE_URL=https://api.example.com/api/v1
```

`VITE_API_BASE_URL` can stay as a placeholder until the EKS API is deployed.
After Phase 7 and Phase 9, change it to the real API URL.

If the frontend uses a custom domain at the root path, use:

```env
VITE_BASE_PATH=/
```

## Deploy Trigger

The workflow runs when:

- code is pushed to `main` and files under `frontend/**` changed
- the workflow is manually triggered with `workflow_dispatch`

## Expected Output

For a normal repository site:

```text
https://<github-user>.github.io/<repo-name>/
```

At the end of this phase, the website can be opened publicly. Login and API
features will work only after `VITE_API_BASE_URL` points to a reachable backend.

## Local Verification

From the repository root, the frontend production build can be checked with:

```powershell
docker build -t traffic-web:phase2 ./frontend
docker run --rm `
  -e VITE_API_BASE_URL=https://api.example.com/api/v1 `
  -e VITE_BASE_PATH=/traffic-forecast/ `
  traffic-web:phase2 npm run build
```

## Phase 2 Done Criteria

- GitHub Actions workflow exists for GitHub Pages.
- Vite build supports repository base path.
- GitHub Pages artifact is produced from `frontend/dist`.
- No backend secret is required for frontend deployment.
