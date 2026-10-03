# Training CSV Data Contract

## Contract version

`traffic-training-csv/v1`

Version 1 intentionally matches the current preprocessing and training
pipeline. Supporting a minimal `date_time,traffic_volume` CSV requires a
separate enrichment design and is outside this contract.

## File requirements

- CSV encoded as UTF-8.
- Header row is required.
- Maximum upload size is configurable; the initial default is 100 MB.
- File name is metadata only. The server generates the storage object name.
- One file contains data for exactly one region.
- Timestamps use the region timezone and must not mix timezones.

## Required columns

| Column | Type | Validation |
| --- | --- | --- |
| `date_time` | datetime string | Parseable, unique, increasing after sort |
| `is_holiday` | string/null | Holiday name or empty/`None` |
| `air_pollution_index` | number | Non-negative |
| `humidity` | number | From 0 to 100 |
| `wind_speed` | number | Non-negative |
| `wind_direction` | number | From 0 to 360 inclusive |
| `visibility_in_miles` | number | Non-negative |
| `dew_point` | number | Finite |
| `temperature` | number | Finite; unit must be consistent in one dataset |
| `rain_p_h` | number | Non-negative |
| `snow_p_h` | number | Non-negative |
| `clouds_all` | number | From 0 to 100 |
| `weather_type` | string | Non-empty when observed |
| `weather_description` | string | Non-empty when observed |
| `traffic_volume` | number | Non-negative target |

Unexpected columns are retained in the raw object but ignored by contract v1
training.

## Validation levels

### Upload validation

- Authenticated admin and valid region are required.
- File extension must be `.csv`.
- File must not exceed the configured size.
- SHA-256 is calculated while streaming to MinIO.
- A region cannot register the same SHA-256 twice.

### Schema validation

- All required columns must exist.
- Values must satisfy the type and range rules.
- Invalid timestamps and duplicate timestamps reject the dataset.
- The dataset must contain at least 4,380 hourly rows after normalization,
  approximately six months.

### Quality validation

- Data is sorted and normalized to one row per hour.
- Missing hourly timestamps are inserted by preprocessing.
- Missing-hour ratio must be at most 5%.
- A consecutive missing gap must not exceed 168 hours.
- Missing feature values may be imputed and must be reported.
- Missing or imputed targets must be marked in the audit artifact.

Schema failures set `validation_failed`. Quality threshold failures also set
`validation_failed`. Warnings that remain within thresholds are recorded in
the quality report and allow the dataset to become `valid`.

## Quality report

The report must include:

```json
{
  "contract_version": "traffic-training-csv/v1",
  "rows_raw": 10000,
  "rows_hourly": 10020,
  "start": "2025-01-01T00:00:00+07:00",
  "end": "2026-02-22T11:00:00+07:00",
  "duplicate_timestamps": 0,
  "missing_hours": 20,
  "missing_hour_ratio": 0.001996,
  "maximum_gap_hours": 3,
  "warnings": []
}
```

## Examples

- `docs/phase-0/examples/valid-row-format.csv` demonstrates valid headers and
  row values. It is intentionally abbreviated and does not satisfy the
  4,380-row minimum for a real training upload.
- `docs/phase-0/examples/invalid-dataset.csv`
