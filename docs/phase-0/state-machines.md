# State Machines

## Dataset

```text
uploaded -> validating -> valid -> processing -> ready
                    \-> validation_failed

uploaded | valid | validation_failed | ready -> archived
```

- `uploaded`: raw object and metadata exist.
- `validating`: schema and quality checks are running.
- `validation_failed`: errors are available to the admin.
- `valid`: accepted for processing.
- `processing`: hourly/features artifacts are being created.
- `ready`: may be used by a training run.
- `archived`: retained for lineage but unavailable for new runs.

## Training run

```text
queued -> validating -> preprocessing -> training -> evaluating -> completed
   \           \              \             \            \-> failed
    \----------------------------------------------------> cancelled
```

- `completed` requires model-version records and a recommendation.
- Failure and cancellation never change the active model.
- A retry continues the same run when idempotency is guaranteed; otherwise a
  new run is created.

## Model version

```text
candidate -> active -> archived
candidate -> archived
archived  -> active
candidate -> failed
```

- `candidate`: registered and available for review.
- `active`: selected for region inference.
- `archived`: retained and eligible for rollback.
- `failed`: invalid artifact and never eligible for activation.

## User

```text
active <-> inactive
```

- New registrations receive role `user`.
- Only admins may change roles or account activity.
- The system prevents removal or deactivation of the last active admin.

## Audit requirements

The following actions create audit logs:

- User role or activity changes.
- Region creation, update, activation, or deactivation.
- Dataset upload and archive.
- Training request and cancellation.
- Model activation and rollback.
