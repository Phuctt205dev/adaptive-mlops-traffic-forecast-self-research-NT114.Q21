# Phase 8: Connect GitHub Pages Frontend

This phase rebuilds the GitHub Pages frontend so browser requests go to the live
k3s API over HTTPS.

## Current Target

```text
Frontend: https://phuctt205dev.github.io/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/
API     : https://api.traffic-mlops.webredirect.org/api/v1
```

## Step 1: Verify API HTTPS

From your machine or Node 1:

```bash
curl https://api.traffic-mlops.webredirect.org/api/v1/health/ready
```

Expected:

```json
{"status":"ready", "...":"..."}
```

## Step 2: Update GitHub Repository Variables

Open:

```text
GitHub repo -> Settings -> Secrets and variables -> Actions -> Variables
```

Set these repository variables:

```text
VITE_API_BASE_URL=https://api.traffic-mlops.webredirect.org/api/v1
VITE_BASE_PATH=/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/
```

`VITE_BASE_PATH` must keep the trailing `/`.

## Step 3: Rebuild GitHub Pages

Open:

```text
Actions -> Deploy Frontend To GitHub Pages -> Run workflow
```

Use:

```text
api_base_url = https://api.traffic-mlops.webredirect.org/api/v1
base_path    = /adaptive-mlops-traffic-forecast-self-research-NT114.Q21/
```

You can also leave the fields empty if the repository variables above are
already correct.

## Step 4: Verify In Browser

Open:

```text
https://phuctt205dev.github.io/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/
```

Use hard refresh if the old build appears:

```text
Ctrl + F5
```

Then login with the bootstrap admin account and confirm the Network tab calls:

```text
https://api.traffic-mlops.webredirect.org/api/v1/auth/login
```

not:

```text
https://api.example.com
http://localhost
```

## Done Criteria

- GitHub Pages loads the current management website.
- Login works from the public GitHub Pages URL.
- API requests use `https://api.traffic-mlops.webredirect.org/api/v1`.
- No browser mixed-content or CORS error appears.
