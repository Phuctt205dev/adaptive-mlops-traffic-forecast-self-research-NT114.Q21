# Current System Assessment

## Available capabilities

| Area | Current state |
| --- | --- |
| API | FastAPI application in `app.py` |
| Web UI | One static prediction page in `docs/index.html` |
| Tree models | Random Forest, XGBoost, and LightGBM |
| Neural models | LSTM and GRU |
| Evaluation | Expanding-window time-series cross-validation |
| Selection | Eight variants ranked by CV mean MAE |
| Champion storage | Versioned copies under `models/champion/` |
| Drift worker | Champion/candidate comparison in `retrain_job.py` |
| MLflow | Used by the legacy pipeline with a local file store |
| Tests | Unit tests for preprocessing, splitting, training, and lifecycle |

## Gaps

- No user database, authentication, or admin/user authorization.
- No region entity and no isolation between datasets or artifacts by region.
- API inference always loads `models/best_model.pkl`.
- The newer champion under `models/champion/` is not connected to the API.
- Current split boundaries are fixed to dates in 2015 and 2016.
- Training scripts write to shared output paths, so concurrent runs can
  overwrite each other.
- No upload workflow, object storage, dataset metadata, or validation status.
- No Airflow DAG or DVC configuration.
- MLflow is not deployed as a shared tracking server and registry.

## Existing behavior to preserve

- Time order is never shuffled.
- Model selection uses CV mean MAE, not Final Test MAE.
- Production data is excluded from offline model selection.
- Lag and rolling features are causal.
- Model promotion is explicit and auditable.

## Required later refactors

1. Replace fixed date splits in `src/time_series_splits.py` with the ratio
   policy defined in `ml/split-policy.md`.
2. Pass region, dataset, and training-run context into every pipeline stage.
3. Replace shared local output paths with scoped object-storage URIs.
4. Resolve the active model by region during inference.
5. Connect the eight-model training path to MLflow and Airflow.
