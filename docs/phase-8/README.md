# Phase 8 - Prediction API Theo Region

## Implemented

- Authenticated prediction endpoint for users and admins.
- Prediction uses the active model version configured on the selected region.
- Model artifact is loaded from the `model_versions.artifact_uri` path.
- Input features are transformed with the same preprocessing logic used by the
  legacy single-model prediction flow.
- Every prediction is persisted to the `predictions` table with the model
  version and feature snapshot used for auditability.

## Endpoint

```text
POST /api/v1/regions/{region_id}/predictions
```

Required:

```json
{
  "forecast_for": "2026-06-19T08:00:00+07:00"
}
```

Optional weather and context features:

```json
{
  "forecast_for": "2026-06-19T08:00:00+07:00",
  "forecast_mode": "future_time",
  "air_pollution_index": 121,
  "humidity": 70,
  "wind_speed": 5,
  "wind_direction": 180,
  "visibility_in_miles": 10,
  "dew_point": 10,
  "temperature": 28,
  "rain_p_h": 0,
  "snow_p_h": 0,
  "clouds_all": 30,
  "weather_type": "Clouds",
  "weather_description": "manual_or_default",
  "is_holiday": null
}
```

## Example

```powershell
$login = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/auth/login" `
  -ContentType "application/json" `
  -Body '{"email":"admin@example.com","password":"admin123456"}'

$headers = @{ Authorization = "Bearer $($login.access_token)" }

$body = @{
  forecast_for = "2026-06-19T08:00:00+07:00"
  humidity = 70
  temperature = 28
  weather_type = "Clouds"
} | ConvertTo-Json -Compress

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/regions/<region_id>/predictions" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

## Notes

- If a region has no active model, the API returns
  `active_model_not_configured`.
- Admin can activate a candidate model with:

```text
POST /api/v1/admin/model-versions/{model_version_id}/activate
```
