# Phase 9 - React Management Website

## Implemented

- Vite React management website in `frontend/`.
- Login and registration screens.
- Role-aware navigation:
  - `admin`: dashboard, user management, regions, datasets, models, prediction.
  - `user`: dashboard and prediction.
- Dashboard with region, dataset and model summary.
- Region management for admins.
- Dataset CSV upload for admins.
- Training trigger through Airflow-backed backend API.
- Model version list and manual activation.
- Region prediction form using the active model version.

## Run

```powershell
docker compose up -d traffic-web
```

Open:

```text
http://localhost:5173
```

Default admin:

```text
admin@example.com
admin123456
```

## Local Development

If using local Node instead of Docker:

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

The frontend calls:

```text
http://localhost:8000/api/v1
```

Override with:

```powershell
$env:VITE_API_BASE_URL="http://localhost:8000/api/v1"
```

## Main Screens

- Dashboard: overview of regions, datasets, model versions and active model.
- Regions: create/update active state.
- Datasets: upload CSV and trigger training DAG.
- Models: inspect model versions and activate a candidate manually.
- Prediction: choose region and request a traffic forecast.
- Users: admin-only user role and active-state management.
