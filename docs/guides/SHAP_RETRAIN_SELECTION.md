# SHAP feature selection during drift retraining

Implementation prepared 29–30 October 2026; operator deployment and real dataset
acceptance remain separate. This change extends the existing pipeline. It does
not extend the Feature Importance page or add periodic evaluation.

## Behavior

- Initial dataset training remains full-feature training plus the existing
  saved SHAP report. It does not run the selection comparison.
- Manual Drift → Retrain and automatic drift retraining build the same trusted
  server policy. The browser does not supply a feature ranking.
- With no validated per-model profile, unavailable quality, or degraded quality,
  retraining evaluates full features and one SHAP-proposed reduced alternative
  inside each chronological training fold. Only training inputs/background enter
  SHAP. The next validation period compares the alternatives; Final Test does not
  select features, epochs, model family or the winning algorithm.
  Neural comparison uses the last 10% of each training fold for early stopping
  on both alternatives, so even pilot weights/epochs do not consume the outer
  validation labels before SHAP ranking. Full sequence arrays are released when
  replaced by reduced tensors, rather than retained alongside them.
- The proposed subset keeps groups covering 95% of training SHAP magnitude.
  Calendar sin/cos and one-hot columns for the same input stay grouped. Accept a
  reduction only when mean validation MAE is at most 2% above the full baseline;
  otherwise retain full features. The final subset comes from the last training
  fold, not from an explanation of the model fit on all Development.
- A valid cached profile may be reused without repeating the full/reduced fit
  comparison. Its algorithm, input groups and (for neural models) sequence length
  must match. Its selection timestamp must precede the first validation fold.
  Overlapping rolling windows often fail this guard and require fresh selection;
  reuse is not a guaranteed speedup. One model's profile never selects another's
  inputs. Missing model-specific profiles are refreshed independently.
- Tree branches still register their winner; they now record separate selection
  results for every selected tree algorithm. Neural branches keep separate LSTM
  and GRU results. Finalize merges all profiles into the training run even if a
  different algorithm becomes the single registered candidate.
- Candidate registration does not activate it. Original CSV/input data is kept.

## Quality policy

The user accepted a **15% MAE increase**, **24 paired hours minimum** and **80%
coverage minimum**. Settings are `RETRAIN_QUALITY_MAE_INCREASE`,
`RETRAIN_QUALITY_MIN_HOURS`, `RETRAIN_QUALITY_MIN_COVERAGE`.

Compare two adjacent post-training replay windows, each up to 168 hours, using
the same active model and dataset. The current window ends before the new
candidate's reserved holdout. If there is not enough post-training data before
that boundary, select the full-feature refresh path. Workers independently check
the actual holdout boundary before honoring a reuse decision. A zero baseline
MAE is handled without dividing by zero.

This is dataset replay against `traffic_volume`, potentially synthetic. It is
not a claim of measured live sensor quality. No periodic scheduler, new alerting
page or quality-only automatic retrain trigger is introduced. Existing drift
thresholds, running-run checks and cooldown still control automatic dispatch.

## Execution, storage and compatibility

Cloud requests run training, selection SHAP, comparison and final explanation
on Kaggle. The internal workers receive/validate artifacts and record metadata.
Local requests use the same pure runner internally. Automatic retrain inherits
the active source run's backend (`local` for legacy runs without a backend);
Kaggle unavailable is an explicit failure, never a local fallback. Controlled
dispatch failures close the execution state instead of leaving it queued.

Store `feature_profiles`, `feature_selection_result` and `execution_cost` with
the run. Profiles include full/reduced CV MAE, per-fold boundaries, selection
groups, final column order and failure/fallback status. Execution seconds cover
preparation/CV/selection/final fit/test/SHAP for a branch, not cloud queue/network
time. Linux process peak RSS is a process high-water mark, not GPU RAM.

Reduced tree models use a built-in sklearn ColumnTransformer/Pipeline while
retaining the full input schema expected by serving. Neural preprocessors store
selected channel indices and names; both training and serving apply the same
mask. Legacy models without selection metadata keep their original behavior.
SHAP report endpoints continue to read saved JSON only.

## Deploy

The original package `results/shap-retrain/review-v3` was applied by the
operator. The first real retrain exposed a mixed numeric/boolean pandas dtype
case in selection SHAP: NumPy inferred object dtype and isfinite raised TypeError.
The correction explicitly converts only sampled rows to float64 (trees) or
float32 (neural), including background samples. It does not allocate a numeric
copy of the entire dataset. Regression tests now cover real weather one-hot
preprocessing and mixed dtypes for all three tree explainers.

The dtype correction was applied. A subsequent real rolling retrain exposed a
separate neural issue: the source timeline was cut at train_start_date even
though the first sequence needs earlier observations. The current correction
keeps sequence_length + 168h of prior causal context, while fitting feature
scalers/encoders only from the first needed sequence hour through training
history. Targets remain inside the selected window. Initial windows at the
dataset start do not acquire invented history.

The context package was applied. It also preserves
normalized child error type/source basename/line in provider.failure_diagnostic.
It never sends raw exception messages, absolute paths or dataset values. A known
ML failure is reported as training_failed rather than cloud_unavailable; unknown
provider failures retain their existing classification. Preserve previous
packages and proofs; do not replay their Apply after source changes.

The current package is `results/shap-retrain/neural-metrics-v1`. A real reduced
neural run exposed missing WAPE/R2 in the replacement CV metric dictionary.
The corrected fold records retain all five metrics from the reduced model,
including in the reuse path. They never substitute WAPE/R2 from the full model
or fill missing values with zero. The full GPU probe now covers two CV folds,
final fit, saved SHAP report and model/preprocessor reload for both LSTM and GRU.

An initial-training attempt was separately confirmed to have feature_selection
unset. It ended in the notebook supervisor without a child exception packet;
the old logs cannot establish its exact cause. The supervisor now retries
transient control GET failures up to three times, bounds HTTP timeouts, and
records safe supervisor phase/type/location/HTTP status and the child exit code
observed before issuing stop. Identity validation and lease expiry still stop
the child. These read retries never create or restart a training job. Initial
model training stays full-feature plus SHAP.

From the repository root, with training idle, the **operator** runs:

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py -B -X utf8 scripts/shap/rollout_neural_metrics_fix.py Check
& $py -B -X utf8 scripts/shap/rollout_neural_metrics_fix.py Apply
& $py -B -X utf8 scripts/shap/rollout_neural_metrics_fix.py Verify
```

Apply updates only the reviewed cloud-code volume on the tree worker, recurrent
worker and API, checks their exact specs, and preserves the image/dependencies,
DB schema and DAG graph. It briefly pauses/resumes training dispatch through the
existing guarded operator. It performs no fit, data deletion, activation or Git
publication. No frontend publish is needed for this backend change.

Rollback, only against this exact reviewed before/after state:

```powershell
& $py -B -X utf8 scripts/shap/rollout_neural_metrics_fix.py Rollback
```

Rollback changes source, not business data. The operator refuses rollback while
any registered reduced-channel neural model still requires the new serving
code, including candidates that could be activated later. Keep compatible
serving code until those models are retired; rollback does not delete them.

## Operator acceptance after deployment

1. Existing active model: verify Prediction and Feature Importance still work.
2. Initial training, when intentionally requested: verify no selection profiles
   and a normal SHAP report; do not launch it just to check deployment health.
3. On an intentional drift retrain, choose local/cloud and selected algorithms.
   Inspect `feature_selection.decision`, per-model `feature_profiles` and cost
   metadata in the resulting training configuration; confirm all selected model
   branches finished and only the normal candidate was registered.
4. Verify each profile's training boundary precedes validation, report samples
   avoid Final Test, full fallback reasons are explicit, and CPU reload checks
   pass. Activate only by the existing admin action when its result is suitable.

Local tests use synthetic fixtures, including real tiny RF/XGB/LGB SHAP. The
runtime compatibility checker uses tiny untrained LSTM/GRU models, real SHAP,
selected-channel preprocessor roundtrip and save/reload predictions on worker
TF2.21. Separately, the user authorized tiny real GPU fits (40 synthetic
sequences, 6 steps, 3 features, one epoch per fit) to test the actual neural fold
on Kaggle TF2.20/Keras3.13. This covers the folding/context path and is still not
a benchmark or acceptance of the real project dataset.
