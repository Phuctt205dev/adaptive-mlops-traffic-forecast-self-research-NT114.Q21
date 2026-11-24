# Local and Kaggle training

This update restores Local for Dataset training. Manual and automatic Drift
retraining always use Kaggle. Existing runs retain their original backend.
Default backend remains Kaggle; switching before submit preserves dates,
model selection and training parameters. Closing the picker does not stop a run.
Each picker submission carries a stable request UUID. The API serializes that
UUID per admin using a PostgreSQL advisory lock and reuses the existing run
after a lost HTTP reply. Reusing a UUID with different parameters is rejected;
the same Airflow run is not dispatched twice. Retry actions use a new UUID and
retain it across transport retries. These are JSON metadata fields, not new DB
tables or columns.
Published neural results store midnight datetime strings for the training
window. Replayed submissions compare those saved values as calendar dates,
preserving the stored metadata. Changed parameters or saved windows that
cannot be compared safely return a conflict. New requests require date-only
input, including when a request UUID is supplied.

## Execution

Kaggle input-upload recovery is separate from starting a notebook. A transient
upload error first checks the same private dataset/version. Missing metadata
alone is not proof of absence: two or more successful absence observations
at least 15 seconds apart and a confirmed absent notebook are required before
retrying the same create-only dataset slug. The total upload budget is three
attempts, persisted before each write, within a persisted 10 minute input
deadline. Confirmed uploads are never uploaded again while processing.
No dataset versions, replacement attempts or extra notebook fits are created.
An existing/maybe-started notebook, an unknown presence result or exhausted
confirmation deadline keeps the safety fence. Proven absence after all upload
attempts fails the branch through normal Stop/cleanup; it does not remain
silently queued. Stop is checked during every backoff. Training Monitor shows
"Checking uploaded training data" during reconciliation, without claiming ML
progress. This recovery runs inside an owned attempt before its notebook starts;
it does not resume an older `needs_attention` attempt after controller replacement.

The [official Kaggle dataset metadata contract](https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets_metadata.md)
defines create with a unique owner/slug separately from creating a new dataset
version. Retries preserve that original create-only identity and source snapshot.

The existing controlled graph remains validate → tree/LSTM/GRU → finalize.
Controllers run no fits. Local creates **one Kubernetes Job per selected model**.
The tree controller runs its three algorithms sequentially; independent neural
branches can overlap with tree work when resources permit. Each Job uses the
same pure ML entrypoint, chronological CV, holdout ratio, random seed, neural
parameters and SHAP policy as Kaggle. Local neural execution uses TensorFlow CPU;
On Kaggle, LSTM/GRU use a GPU by default. Advanced → "Kaggle hardware" (request field
`neural_accelerator`, `gpu` or `cpu`) lets an admin ask for a CPU notebook instead: it never
waits for a GPU, and the notebook may use up to four cores instead of one. The choice is part
of the stored, hashed run configuration. Tree work is always CPU, Local ignores the field, a run
saved before the field existed keeps asking for a GPU, and automatic retrain keeps the hardware
of the run it follows. A GPU request still fails on a machine without a GPU; a CPU request does not need one.

Local admission holds PostgreSQL advisory transaction lock `73918046701` while
checking capacity and reserving one node. Existing Pending Jobs also reserve
their hostname. Node01 is excluded. Required pod anti-affinity permits at most
one training Pod per hostname in `traffic-mlops`, hence at most two fits across
node02/03. A free node is re-evaluated for the next model; jobs are not bound to
the node hosting their controller.

Admission requires fresh node CPU/memory metrics (up to 90 seconds), Ready,
no memory/disk pressure, enough memory above the configured reserve, and at
least 0.5 additional CPU core above the requested CPU. Kubernetes scheduling
still checks requests. Requests and limits reserve the same CPU/RAM; ephemeral
storage requests/limits are 4Gi. Default per model: 1 CPU, 3072Mi RAM, 1536Mi
additional memory reserve, 8-hour deadline including branch queueing. Both
nodes might not qualify at once. Queueing does not make an oversized model fit.

Threads (joblib/OpenMP/BLAS/TensorFlow) are bounded. No CV/epochs/features/SHAP
reduction is applied to fit the budget. Memory exhaustion fails a Job; it does
not pause its running process or move its RAM to another node. There is no
distributed single-model execution or cross-backend checkpoint resume here.

Inputs are immutable, hash-checked snapshots of dataset/configuration/pure ML
source, uploaded under canonical run/branch/attempt prefixes. The CPU image is
pinned by digest. Job names are deterministic and reconciled after conflicts;
there is no Kubernetes fit retry (`backoffLimit: 0`). Source archives are size-
bounded and path-checked. Jobs upload output files first and the manifest last.
Controllers verify all hashes and reload compatibility before the existing
receipt/finalize gate can register candidates. Three tree Jobs produce one CV
winner with all selected tree SHAP reports, preserving the existing contract.

## Stop and recovery

Admin Training Monitor has Stop training with an application confirmation
dialog. The server sets durable cancellation intent. Unstarted work is fenced;
claimed work remains Cancelling until every branch confirms stop **and** cleanup.
Admin cancellation ends as Cancelled, distinct from failure. The existing DB
TrainingRunStatus already includes cancelled; no new enum value is introduced.
This release does not train or activate anything by deployment alone.

Local foreground-deletes only identity-matched Jobs using UID preconditions,
waits for Pod removal, then deletes attempt-owned temporary objects/files.
Restart recovery uses the persisted Job identities; it never silently refits a
lost Job. Existing run/execution state remains visible to the delivery guard.

Kaggle status/control read-back reconnects with bounded backoff and jitter.
Ambiguous submit/control writes are reconciled rather than repeated. For a lease
renewal (Quick Save, never a new run) whose reply was lost, the worker first reads the
notebook again three times, since a write can commit late. Only if the notebook is
exactly as before and the error was a transient network failure does it repeat the
same write once, with identical content; any other change stops the repeat. An
unconfirmed renewal is read again every few seconds until its lease is nearly over,
instead of ending the branch at once. Kaggle also refuses an occasional Quick Save and
accepts the next one seconds later (seen 24/11, each time on the first renewal). A refused
**renewal** changed nothing, so while the current lease has more than 45 seconds left it is
asked again after about 5 seconds; four refusals in a row, or a nearly expired lease, end the
branch as before. A refused **stop signal** is never tolerated. The refusal text itself is never
kept: only words from a fixed allow-list (for example `running`, `limit`, `quota`) are stored as
`diagnostic.hint` in the provider journal, so the reason can be recognised later. Download
retry cleans only attempt-owned partial output and downloads the same version;
it does not retrain. Native session cancellation is used at most once when the
original SaveKernel response explicitly linked a session ID for this notebook;
otherwise cooperative cancellation remains the fallback. Neither response to
Cancel nor lease expiry is treated as proof of stop.

Training Monitor labels a notebook queued on Kaggle with its wait. After 15 minutes it
adds the notebook name to cancel on kaggle.com and points to the CPU option; Kaggle gives
no reason or position, and the system cannot cancel a queued session itself.

When the outcome remains unknown, the worker keeps the original attempt and
execution fence in Needs attention. Airflow timeout does not convert that
outcome into a confirmed remote failure. Admin can request stop; retry/fallback
stays disabled until terminal state and cleanup are verified. This first update
does not offer manual resume after the reconnect budget has expired.

Retry creates a new run linked through `retry_of`, using the original dataset,
window, model selection and configuration. The API rejects changed parameters,
an unresolved source execution, and an already active/successful retry. Local
fallback is an explicit admin choice for Dataset training only. Retrain cannot
switch to Local at API/service/DAG boundaries. No completed partial model is
reused across attempts; no model is automatically activated.

## CI/CD integration handover — required before enabling Local

The component pipeline packages/builds/reuses the **execution image** from
`deployment/cicd/components.json` and binds its immutable digest to API/worker
configuration. Runtime ServiceAccounts require a reviewed one-time owner
bootstrap. `deployment/cicd/local-training.json` starts disabled; merged code or
successful image checks do not establish that Local is enabled on the cluster.
See [component delivery](COMPONENT_DELIVERY.md) for owner Plan/Apply commands.

1. The pipeline builds `infra/local-training/Dockerfile` from an isolated root
   context containing its import closure and recursively included dependencies,
   checks offline, publishes, and binds `ghcr.io/...@sha256:...`. Dependencies come from
   `requirements/local-training.txt` and the existing role constraints. Check
   imports for all three tree libraries, TensorFlow CPU, SHAP, MLflow and boto3;
   preserve compatibility with current serving-role dependency versions.
   The image needs node disk headroom for its layers and working data. Do not
   infer disk or RAM fitness from idle measurements or bypass existing capacity
   checks. Do not add real Kaggle training or kind/full to every push.
2. Review/apply `deployment/rke2/local-training-rbac.yaml` once through the
   authorized infrastructure workflow. It adds a dispatcher SA and an unprivileged
   Job SA. It grants no Secret-reading, node proxy, node mutation, or access to
   other namespaces. Keep delivery credentials separate from runtime credentials.
3. The reviewed renderer configures both worker role templates (all slots),
   only when Local is enabled, to use
   `serviceAccountName: traffic-training-dispatcher` and
   `automountServiceAccountToken: true`. Job Pods themselves keep token mounting
   disabled and use `traffic-local-training`. Review the necessary read/list of
   nodes/metrics and create/get/list/delete Jobs, get/list Pods and Pod logs.
   Do not patch only a live Deployment that the next release will overwrite.
4. Set the corresponding fields in `deployment/cicd/local-training.json` and
   use the normal reviewed release. The renderer synchronizes these settings
   on **API and both workers**, initially keeping Local disabled:

   | Variable | Value |
   | --- | --- |
   | `LOCAL_TRAINING_ENABLED` | `false` until bootstrap/verification is complete |
   | `LOCAL_TRAINING_IMAGE` | Published immutable image digest |
   | `LOCAL_TRAINING_NAMESPACE` | `traffic-mlops` |
   | `LOCAL_TRAINING_MEMORY_MIB` | `3072` initial budget; profile before changing |
   | `LOCAL_TRAINING_CPU` | `1` (supported: 1 or 2) |
   | `LOCAL_TRAINING_RESERVE_MIB` | `1536` (minimum 1024) |
   | `LOCAL_TRAINING_DEADLINE_SECONDS` | `28800` |
   | `LOCAL_TRAINING_SERVICE_ACCOUNT` | `traffic-local-training` |

   Existing `TRAINING_CONTROL_ENABLED`, Kaggle flags, internal tokens and S3
   settings remain. Job inputs/outputs use the existing artifact bucket. The
   Job references the current `traffic-secrets` MinIO credentials; no credentials
   are embedded in Job journals, source bundles or browser responses.
5. Preflight the exact image, SA/RBAC, metrics and node storage, then enable
   Local through the release configuration. Check each worker's
   `/health/local-training` and API `/api/v1/admin/training-monitor/capabilities`.
   The API enables Local only when both workers report readiness with the same
   image digest. Missing RBAC/token/metrics keeps it unavailable. Busy resources
   do not disable Local: accepted work queues until a node qualifies.
6. Keep the current active-training delivery fence. All tracked Local Jobs
   correspond to running executions; do not deploy around unresolved or orphan
   Jobs by editing their state. Check Job identities during lifecycle/rollback
   verification. No support is claimed for replacing controllers during active
   training without stopping/reconciling their work.

## Verification and acceptance

Automated tests use isolated SQLite, fake Kubernetes/S3/Kaggle, and synthetic ML
fixtures. `tests/test_local_training.py` exercises five individual Jobs and tree
report merging; `test_training_recovery.py` tests cancellation and retry lineage.
Existing ML tests and runner tests cover shared configuration/SHAP. Browser
check: `frontend/tests/check_local_cloud_training_ui.py` against built assets,
all APIs intercepted; screenshots/proof go to ignored results.

After deployment and infrastructure integration, the owner performs real
acceptance: one selected tree, each deep model, then all five on Local; then
all five on Kaggle. Check CV/final training/SHAP/result verification, candidates,
Feature Importance, cancellation, queue reasons, existing predictions and
CPU/RAM/disk throughout. Qualify parallelism with one deep plus one tree before
two deep models. Compare identical data/configuration; GPU/CPU need not produce
bit-for-bit identical results. No five-model production training or resource
benchmark has been performed as part of writing this feature.
