# Chronological Split Policy

## Policy version

`chronological-70-15-15/v1`

## Preconditions

The split runs after the dataset has been validated, sorted by `date_time`,
and normalized into one continuous hourly timeline.

Let `N` be the number of normalized hourly rows:

```text
development_end_index = floor(N * 0.70)
final_test_end_index  = floor(N * 0.85)

Development = rows [0, development_end_index)
Final Test  = rows [development_end_index, final_test_end_index)
Production  = rows [final_test_end_index, N)
```

The partitions are contiguous and do not overlap. Their actual row counts may
differ from exact percentages by at most one row because of integer rounding.

## Partition purposes

| Partition | Purpose | Allowed for fitting/tuning |
| --- | --- | --- |
| Development, first 70% | Expanding-window CV and final candidate fitting | Yes |
| Final Test, next 15% | One-time unbiased reporting after selection | No tuning |
| Production, last 15% | Drift and promotion simulation | Never |

Final Test metrics must not affect ranking. Production must never be used to
fit, tune, rank, or choose the initial recommended model.

## Cross-validation

- Cross-validation runs only inside Development.
- Default number of folds: 5.
- Fold strategy: expanding window.
- Data is never shuffled.
- Every training timestamp must be earlier than every validation timestamp in
  the same fold.
- Preprocessors and feature transformations are fitted separately using only
  each fold's training partition.
- Lag and recurrent sequences may use history before a validation timestamp,
  but never targets from that timestamp or the future.

## Minimum data

- Minimum normalized timeline: 4,380 hours.
- Minimum continuous history required for lag/LSTM/GRU variants: 168 hours.
- All three top-level partitions and all CV folds must be non-empty.
- A run that cannot satisfy the requested folds or sequence length fails
  before model fitting with an actionable validation error.

## Reporting

Every training report records:

- Policy version and configured ratios.
- Row count and time range for each top-level partition.
- Row count and time range for every CV fold.
- Proof that Production was not used for training or selection.
