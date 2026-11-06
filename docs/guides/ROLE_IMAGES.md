# Role images and delivery

The application release has four immutable images in the existing public GHCR
`backend` package. CI builds only a changed content identity and checks every
image, including a reused image, against the current offline producer fixtures.

| Image | Runtime responsibility | Excluded implementation |
|---|---|---|
| api | HTTP, inference for all five models, quality/reports, Kaggle orchestration | Worker executor, Kaggle SDK, project fit modules, SHAP engine |
| tree | Kaggle tree branch transport, verification and receipt finalization | TensorFlow/Keras, neural fitting in the server import graph |
| neural | Kaggle LSTM/GRU transport and CPU artifact verification | XGBoost, LightGBM, tree fitting in the server import graph |
| sync | Cache restore/upload and deployment lease | FastAPI, NumPy/Pandas, ML runtimes |

Workers retain a separate `/app/kaggle-project` source snapshot for packaging
the Kaggle job and a read-only CPU verification subprocess. It is not on the
server import path. Their HTTP gate accepts the existing controlled Kaggle
protocol only. Estimator libraries still contain their own fit APIs; those
libraries are required to deserialize and verify artifacts and are not edited.

API/tree use `xgboost-cpu`; API/neural use `tensorflow-cpu`, at the reviewed
producer versions. MLflow uses its supported skinny client. Kaggle SDK remains
in an isolated build-time venv. No worker startup installs SDK/SHAP packages.
Source/package identities and images are pinned; image size is not a RAM claim.

## Cohorts and stable addresses

Active pods use `api`, `train-tree`, `train-neural` on the first migration.
Later releases alternate with `api-next`, `tree-next`, `neural-next`. The
inactive Deployments have zero replicas. Only changed roles are replaced;
changing sync also replaces its API pods. Existing Service addresses and
NodePort are retained, so frontend and Airflow URLs stay stable.

The first bootstrap creates only empty Deployments, `api-preview`, `api-peers`,
a PDB, and named-resource runner patch permissions. `bootstrap_roles.py Plan`
uses server dry-runs; `Apply` is the one-time operator step, after image checks.
It never replaces existing serving workloads or grants Secret reads.

CD v2 pauses the training and drift DAGs, rechecks active work, then obtains a
worker admission lease separate from the serving barrier. Accepted requests
remain durable queued DagRuns. If work is active before mutation, delivery is
**DEFERRED**, not completed; the saved source CI is retried on the 15-minute
hosted schedule only while Auto is enabled, idle and unblocked. Manual mode,
failure latch, provenance checks and transaction ownership remain in force.

CD stages a warm API cohort while both old APIs serve, verifies preview HTTP
and model identity, and stages idle worker cohorts. A short peer-membership
lease protects Service promotion. Only LocalExecutor's scheduler source is
updated; the compatible webserver stays available to acknowledge queued work.
Both application DAGs must be idle; their prior pause states are restored.

After promotion, CD checks endpoints and read-only business calls. The old API
finishes requests and flushes cache before retirement; native sync sidecars
stop after the main container and make a final upload pass. Legacy migration
waits for a quiet TCP-session window and explicitly flushes its old sidecar.
Long requests that exceed the reviewed drain budget stop retirement. Old
worker watchers are retired before queued dispatch resumes.

The PDB only protects supported voluntary evictions. The release also needs
staging capacity and retains old cohorts until checks pass. It cannot prevent
independent failures in the existing single data/ingress services.

## Evidence and recovery

The Actions Summary distinguishes completed, deferred, failed and recovered
outcomes. The sanitized receipt records role source IDs, image digests, ready
pods, business-check counts and an availability observation window. It never
contains user response bodies, credentials or inverse specifications.
Authenticated business probes stay inside the cluster. Public ingress probes
use anonymous live/ready endpoints only; no administrator token crosses the
external tunnel. The user verifies authenticated frontend flows separately.

Write-ahead inverses stay in `role-transactions/` beside the private delivery
kubeconfig, outside the runner checkout and uploaded artifact paths. Each
mutation tests UID/resourceVersion; rollback also requires the exact recorded
after hash. Before writing that hash, CD server-dry-runs the exact patch and
records its defaulted spec (including ConfigMap volume defaultMode). Dry-run
and mutation use the same resourceVersion, so a concurrent edit is rejected.
External drift or a lost lease retains the recovery latch.
Rollback restores resource specifications, never user data or model activation.

Read-only business probes preserve allowlisted endpoint/status/error facts in
the receipt when an exec process would otherwise hide them. The availability
observer and full business probe share a lock: they must not compete for the
prediction runtime while a quality report is checked. User traffic continues;
an actual failed HTTP check still fails the release without retrying it away.

For an interrupted v2 transaction, use the original checked release, binding,
journal and kubeconfig with `roles_deploy.py Rollback`; inspect its result before
`recover_cd.py`. Read-only recovery requires closed guard, exact resource hashes,
ready cohorts and restored DAG pause states. Do not replay Apply or edit a
receipt/state to bypass recovery. Auto remains blocked after a real failure
until the existing approved Manual repair procedure succeeds.

Deployment is verified by bounded checks, not a proof that every possible
request succeeded. Production training, cancellation and activation are not
part of image tests; the user separately checks those frontend operations.
