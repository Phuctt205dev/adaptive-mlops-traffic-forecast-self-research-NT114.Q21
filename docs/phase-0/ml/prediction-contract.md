# Prediction Contract

## Forecast modes

### `next_hour`

Uses lag-based tree models, LSTM, or GRU. The requested time must be exactly
one hour after the latest available region history. At least 168 continuous
hours of history are required.

### `future_time`

Allows an arbitrary future timestamp and uses an active no-lag model. Required
exogenous weather features are obtained for the region coordinates or
provided by an approved feature source.

The API rejects a request when the region's active model is incompatible with
the requested forecast mode.

## Request

```json
{
  "region_id": "uuid",
  "date_time": "2026-06-20T10:00:00+07:00",
  "mode": "future_time"
}
```

`date_time` must include an offset and must agree with the region timezone.

## Response

```json
{
  "prediction_id": "uuid",
  "region_id": "uuid",
  "date_time": "2026-06-20T10:00:00+07:00",
  "mode": "future_time",
  "prediction": 1532.0,
  "unit": "vehicles/hour",
  "model_version_id": "uuid",
  "model_variant": "lightgbm_no_lag",
  "predicted_at": "2026-06-11T22:00:00+07:00"
}
```

## Resolution rules

1. Load the region and require `is_active`.
2. Resolve the region's active model version from PostgreSQL.
3. Require the model status to be `active`.
4. Validate forecast-mode compatibility.
5. Load or reuse the cached immutable model artifact.
6. Build features without using future target values.
7. Store the prediction with the exact model version used.

Changing the active model invalidates that region's model cache.

## Errors

| Condition | HTTP status |
| --- | --- |
| Region not found | 404 |
| Region has no active model | 409 |
| Model incompatible with forecast mode | 422 |
| Insufficient continuous history | 422 |
| Weather/feature source unavailable | 503 |
