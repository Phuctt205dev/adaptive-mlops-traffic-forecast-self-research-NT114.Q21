# Isolated delivery acceptance

`01 Application Delivery / current-runtime` runs on a disposable GitHub-hosted runner,
after the role images pass offline checks and the immutable package is built.
Only an explicit manual `validation=full` or the weekly schedule selects these
clusters; `legacy-migration` then runs independently in parallel. Every push,
including controller/workflow edits, uses fast checks without kind. Acceptance
still requires each selected suite for that exact commit/run/attempt; historical
v1/v2 proofs retain their original policy. See [Actions navigation](../../docs/guides/ACTIONS_DEMO.md).

The script refuses a non-hosted runner, a context other than `kind-cd-e2e`, or
a Kubernetes API outside loopback. It does not read a production kubeconfig,
credentials, dataset, model or namespace. The namespace has the same name in
the separate kind cluster; its UID is obtained after creating it there.

Actual components exercised:

- Pinned Kubernetes 1.36.4, real Deployment/Service reconciliation and server defaults.
- The actual legacy image with its frozen source ConfigMap/import loader.
- The role images bound by this CI, actual restore/upload/flush CLI and native sidecar shutdown.
- PostgreSQL 16 advisory leases and real MinIO object upload/download.
- Real prediction with a tiny offline RandomForest artifact; no Kaggle calls.
- The same Plan/Apply/Verify/Rollback engine used by the Windows wrapper.

HTTP business handlers and the Airflow REST API are explicit fixtures. Actual
Airflow dependency image/DAG imports are used, but the scheduler does not run
training. This test does **not** replace the five-model image checks, API unit
tests, live server Plan, production frontend acceptance, or a future approved
training test. It is not a benchmark or evidence of universal zero downtime.

The test profile adjusts node names, mounts the fixture handler, uses a tiny
data disk reserve, and replaces the log PVC with ephemeral storage. Production
templates/resources remain unchanged. Synthetic secrets and a generated local
TLS key exist only on the ephemeral cluster and are not uploaded as evidence.
The fixture process adds its generated public certificate to the disposable
container's certifi bundle. This lets requests probes which ignore environment
overrides validate the local HTTPS ingress without disabling certificate or
hostname verification; production trust stores are unchanged.
The replacement HTTP fixture also installs SIGTERM handlers because it runs as
PID 1, matching the real ASGI/Airflow servers' ability to stop. Long requests
must still finish before retirement; the controller's grace budgets are kept.

The balanced profile has six current cases: one API manifest-only rollout with
a long request finishing, worker-only rollout preserving API, idle-only deferral,
preview smoke failure, real S3 upload denial after promotion with scheduler
defaulting/rollback, and a final no-op proving recovery converged. The first
case checks identical image bindings and a changed API deployment ID; it replaces
a separate manifest-only rollout. Repeated initial/middle no-op cycles are omitted.

The optional migration suite has only three legacy-specific cases: regular
release rejection on legacy, preflight failure before mutation, and actual
migration with a long request finishing and legacy cache flush. Ordinary recovery
cases are checked in current and are not repeated after migration. Manual full
and weekly validation require both suites. Source changes or unknown history do
not automatically select clusters for release pushes; they retain fast checks
and production preflight/Verify/rollback. This is an explicit coverage choice.
All selected cases and immutable identities are enforced by proof v3.

A synthetic queued TrainingRun/TrainingExecution is stored in real
PostgreSQL and its unchanged queued state is checked after every case. Airflow's
queue REST response is a fixture; no real scheduler is asked to dispatch it.
Only allowlisted receipt/error/pod facts are preserved in
`results/cicd/kubernetes-delivery-proof.json`.

The migration job allows 50 minutes for setup and its three legacy cases. The pinned
legacy migration alone took about 20 minutes in run 42338350630; that run
passed seven cases before its old 35-minute job limit canceled the last update.
Production drain/shutdown budgets are preserved. Phase transitions are logged,
and `kubernetes-delivery-progress.json` is written atomically before and after
each case, so timeouts can preserve the completed results. Its distinct
`traffic-isolated-delivery-progress/v1` schema and `complete: false` never
substitute for the final acceptance proof or a successful overall CI.
