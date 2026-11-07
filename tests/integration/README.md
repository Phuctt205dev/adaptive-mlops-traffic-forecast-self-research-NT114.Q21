# Isolated delivery acceptance

`Backend CI / isolated-delivery` runs on a disposable GitHub-hosted runner,
after the role images pass offline checks and the immutable package is built.
The full CI must succeed before the production provenance gate accepts it.

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

Required cases include legacy preflight failure before mutation, actual
migration with a long request finishing, idle-only deferral, verified no-op,
preview smoke failure, real S3 upload denial after promotion with scheduler
defaulting/rollback, manifest-only update without an image rebuild, and queue
preservation. Only allowlisted receipt/error/pod facts are preserved in
`results/cicd/kubernetes-delivery-proof.json`.
