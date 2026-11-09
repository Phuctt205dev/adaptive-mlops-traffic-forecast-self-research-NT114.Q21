# Training progress and timing

The Dataset, manual Drift and automatic Drift paths use the same controlled
Kaggle execution, progress contract and monitor. This change does not alter
model selection, CV, epochs, feature policy, SHAP, Final Test or activation.
Publication still requires valid receipts from all selected branches.

## Confirmed milestones

- `cv_completed`: cross-validation finished; final fitting/SHAP can still remain.
- `ml_completed`: final training, evaluation and requested SHAP finished.
- `output_verified`: the worker accepted the branch receipt after output and
  serving compatibility checks. It is not run publication.
- `publication_completed`: the registry transaction completed. Successful runs
  remain omitted from the active monitor as before.

The monitor counts these separately. Counts are not an estimate of remaining
time. A queued Airflow branch is different from an observed Kaggle queue.
No ML event means "waiting for progress", not proof that a model is learning.
Heartbeat freshness is separate from ML progress freshness. "Waiting for new
ML progress" means no new event was received recently, not a diagnosed hang.

## Optional event fields

Existing events without timing remain readable. New events carry:

| Field | Meaning |
| --- | --- |
| `source_time` | UTC wall time in the emitting process. |
| `source_clock`, `source_elapsed_seconds` | Monotonic elapsed time within the named reporting scope. Only intervals on the same clock/scope are accumulated. |
| `supervisor_relay_time` | Wall time when the supervisor forwarded a child event. |
| `worker_stream_time` | Wall time when the SDK stream relay received the event. |
| event `time`, model `last_ml_received_at` | Backend acceptance time. |
| `fit_id`, `fit_purpose` | Fit identity within the attempt; `cv_full`, `cv_reduced`, `cv_reused` or `final`. |
| `epochs_completed`, `early_stopped` | Actual observed completion of a neural fit, separate from its configured epoch limit. |

Source/relay/backend wall clocks belong to different processes/hosts. Their
timestamps identify boundaries, but their subtraction is not an authoritative
GPU queue or fit duration. Missing source markers remain unknown. Fit duration
and completed phase durations use the source's monotonic clock. Models retain
up to 32 fit summaries and 48 recent events each; the run retains 500 events.
The run/branch/attempt checks and event sequence fence remain mandatory.

The supervisor forwards validated events from its reader while its control
request is waiting. The worker drains logs on a separate, bounded-lifetime
thread; a control/status RPC does not hold up progress delivery. Stream/context
validation errors still close the run; no progress is invented to animate UI.

## Worker measurements

Normalized provider `telemetry` stays in the existing branch JSON after
cleanup. The monitor exposes only an allowlisted observation, never notebook
source, snapshot contents, private references or signed output URLs.

- At most 24 operation aggregates and 40 first-observed milestones per branch.
- Each operation records count, errors, monotonic seconds, payload bytes
  attempted and payload bytes from successful calls.
- SDK import/auth and SDK operation components are reported for successful RPC
  attempts. The outer operation includes subprocess overhead and read retries.
- Upload byte counts describe the input snapshot, not compressed wire bytes.
  Download counts describe manifest-declared payload sizes. Failed/partial
  transfer bytes are not claimed as successful bytes.
- Packaging, provider observations, runtime preparation, ML, output transfer,
  serving verification, branch publication, registry publication and cleanup
  are distinguishable. Nested operation totals and parallel branches must not
  be added together as end-to-end duration.
- `publication_work_seconds` ends before the registry commit. The tree branch's
  post-commit `finalization` duration includes bridge, registry and commit; its
  optional telemetry write cannot fail or repeat an already-published run.
- Dataset/source/configuration hashes and registered dataset row count identify
  the workload. Registered rows are not the effective offline training rows;
  use the training result/configuration to compare windows and feature policy.

## Control update recovery

Only fixed diagnostic categories are retained from a rejected Quick Save.
Raw upstream messages and invalid input names are discarded. Authentication,
quota and invalid-source failures remain fatal. An unknown rejection does not
become a transient failure by default.

Before a Quick Save, persist its base version and intended source hash, cancel
flag and lease. Read-back must confirm an advanced version and exact source;
a successful response's version must also match. A lost reply is reconciled
without repeating the write. A restart reads the original pending intent.
Only an explicit transient refusal, followed by unchanged exact source/version
and enough remaining lease, permits one additional `no_run=True` write. A
timeout alone never permits retry. The pinned execution version is unchanged.
Observed cancellation is latched. A terminal execution is read/verified without
another Quick Save. Renewal scheduling uses the lease actually written, not
an imagined lease starting when the HTTP reply finally arrives.
Cancel requested, provider terminal, cloud cleanup and run-staging cleanup
remain separate confirmations.

## Package 2: execution overhead and independent neural sessions

Dataset training, manual Drift retraining and automatic Drift retraining share
these changes. The controlled DAG keeps one active run and permits three branch
tasks. Tree, LSTM and GRU start independently and join at the same finalizer.
LSTM and GRU retain separate notebooks, input snapshots, execution versions,
leases, cancellation journals and receipts. Kaggle still decides when a queued
notebook receives GPU capacity; parallel submission does not guarantee parallel
GPU allocation or a particular end-to-end duration.

The neural worker permits two cloud branches of the same run. Its existing
exclusive operation lock remains held until the last owned branch stops; other
runs and lifecycle operations cannot borrow that lock. Pending cancellation or
cleanup continues to fence new work through durable state. CPU serving reload
checks run one at a time to preserve the worker's memory budget. No additional
local model fitting is enabled.

Each attempt uses its own SDK RPC process and authenticated API, rotated after
512 confirmed replies. Every request has a unique response identity and shares
one deadline across bounded read retries. Timeout, invalid identity or broken
IPC closes the process; ambiguous writes are never repeated by this layer.
The existing exact-source/version reconciliation remains authoritative.
Progress uses a separate process, reuses authentication on quiet reconnects and
deduplicates replayed events before they enter the bounded worker queue.

Confirmed queues without ML progress use status polling intervals of
3, 6, 12 and at most 15 seconds, excluding request time. Real progress returns
polling to 3 seconds. Waiting checks cancellation every second and wakes for
progress or lease renewal. Airflow continues its 3-second worker heartbeat;
its log shows phase/provider changes and a quiet status at most every 30 seconds,
without relabeling sibling events or inventing epochs.

Output reception validates the complete manifest first, then downloads at most
three payload files together, using a separate HTTP session per transfer thread.
Only manifest paths on the pinned execution version are accepted; byte limits,
checksums, destination ownership and full bundle verification still apply.
Failure or cancellation stops pending transfers and joins active transfers
before returning. In-flight HTTP reads retain their existing bounded timeout.
Signed URL refreshes are serialized and remain scoped to the same file/version.

`sdk_session_start` and `sdk_session_reuse` count confirmed RPC replies using a
new or reused session; they are not counts of failed authentication attempts.
`output_transfer_wall` measures the payload pool's wall duration, excluding the
manifest and listing. `output_download.seconds` sums individual transfers and
can exceed wall duration when transfers overlap. `verify_serving` includes
waiting for the serial CPU reload slot. These nested timings must not be added
to queue time or compared as disjoint phases.

Live comparison still requires the same dataset, window, CV, epochs, features,
SHAP policy and runtime. Compare provider queue, child ML time, RPC overhead,
output, cleanup and total duration separately; preserve candidate quality and
active-model identity. A past 8–9 minute run with an unknown workload is context,
not a controlled baseline. Use the existing CI/CD engine for release and a
user-created run or the existing automatic schedule for live acceptance.

## Validation and release

Run the backend unit suite (including neural tests in the official CI runtime),
`npm run test:monitor` and `npm run build` in `frontend`. The synthetic browser
check is `scripts/cloud_training/check_training_monitor_ui.py`; it blocks
external requests and all API mutations. It covers event bursts, phase labels,
early stopping, stale ML, failure/blocked branches, cleanup, mobile layouts,
unavailable snapshots and disappearance after successful publication.

Use the existing CI/CD engine, with backend Verify before dependent frontend
publication. A user-created five-model run and the existing Drift schedule
provide live acceptance after release. Do not submit real Kaggle jobs from CI,
activate a candidate or clear Airflow/DB state to manufacture acceptance.
Package 2 is selected from the completed five-model run's phase measurements;
offline validation alone does not establish its production speedup.
