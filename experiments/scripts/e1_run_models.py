"""Step 3.3: train the project's models offline with the project's own code.

Three groups, all with the website's training configuration (as in run f93d0351):
  1. "system" trees: src.pipeline.run_pipeline, exactly what the Kaggle tree branch
     calls (scripts/cloud_training/execute_branch.py). These trees use weather and
     calendar features only (model_family legacy_sklearn). run_pipeline scores the
     final test only for the winning tree, so the same pipeline functions
     (preprocess, split_data, train_*) are reused to get test predictions for all
     three; for the winner they must equal run_pipeline's test MAE.
  2. "system" LSTM/GRU: src.region_training_variants.train_region_recurrent_candidate,
     what the Kaggle LSTM/GRU branches call.
  3. "lag" trees: src.region_training_variants.train_region_tree_lag_candidates, an
     existing code path with traffic lag/rolling features that the website does
     NOT use today (kept for comparison only).
SHAP is switched off (it runs after fitting and does not change the models).
MLflow is not installed, so src.pipeline uses its built-in no-op stand-in.
Nothing touches the database, object storage or the cluster.

Usage (from the repository root):
    python experiments/scripts/e1_run_models.py NAME DATASET_CSV TRAIN_START TRAIN_END WORK_DIR

Writes experiments/outputs/e1/NAME/{summary.json, <model>_final_test_predictions.csv}.
Model files and pipeline side files go to WORK_DIR/NAME (outside the repository).
"""
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "src" / "region_training_variants.py").is_file())
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src.pipeline import evaluate, run_pipeline, train_lightgbm, train_random_forest, train_xgboost  # noqa: E402
from src.preprocess import load_observed_target_data, preprocess, split_data  # noqa: E402
from src.region_training_variants import (  # noqa: E402
    train_region_recurrent_candidate,
    train_region_tree_lag_candidates,
)

def apply_median_round_patch():
    """Experiment-only stand-in for a pending fix (not in the project code).

    build_causal_target_series fills long gaps with a rolling median, which can be
    a half value (e.g. 3456.5) that pandas refuses to write into the Int64 target
    column (pandas 2.3.3 and 3.0.2). Rounding the median keeps whole vehicle counts.
    """
    import src.lstm_sequences
    import src.sequence_serving
    import src.time_series_features as features

    def patched(dataframe):
        observed_target = dataframe[features.TARGET_COLUMN].where(dataframe["target_observed"])
        causal_target = observed_target.copy()
        causal_target = causal_target.fillna(observed_target.shift(168))
        causal_target = causal_target.fillna(observed_target.shift(24))
        causal_target = causal_target.fillna(
            observed_target.shift(1).rolling(window=168, min_periods=1).median().round())
        causal_target = causal_target.ffill()
        if causal_target.isna().any():
            raise ValueError("Không thể tạo causal target vì đầu chuỗi không có target thật.")
        return causal_target

    for module in (features, src.lstm_sequences, src.sequence_serving):
        module.build_causal_target_series = patched


TREES = ["random_forest_lag", "xgboost_lag", "lightgbm_lag"]
MODELS = [*TREES, "lstm", "gru"]
TRAINERS = {"RandomForest": train_random_forest, "XGBoost": train_xgboost, "LightGBM": train_lightgbm}


def system_config(train_start, train_end):
    """Same values as the website's training configuration (run f93d0351), SHAP off."""
    return {
        "train_start_date": train_start, "train_end_date": train_end,
        "cv_splits": 3, "random_state": 42, "selected_models": MODELS,
        "recurrent_sequence_length": 72, "recurrent_epochs": 3, "recurrent_batch_size": 32,
        "final_test_ratio": 0.15, "model_role": "candidate", "shap": {"enabled": False},
    }


def versions():
    import lightgbm, numpy, sklearn, xgboost
    result = {"python": platform.python_version(), "numpy": numpy.__version__, "pandas": pd.__version__,
              "scikit-learn": sklearn.__version__, "xgboost": xgboost.__version__, "lightgbm": lightgbm.__version__}
    try:
        import tensorflow
        result["tensorflow"] = tensorflow.__version__
    except ImportError:
        pass
    return result


def save_predictions(path, timestamps, actual, predictions):
    pd.DataFrame({"date_time": pd.to_datetime(timestamps).to_numpy(), "actual": list(actual),
                  "prediction": list(predictions)}).to_csv(path, index=False, lineterminator="\n")


def system_trees(data_path, config, work, out):
    started = time.perf_counter()
    info = run_pipeline(
        train_start_date=config["train_start_date"], train_end_date=config["train_end_date"],
        model_role="candidate", random_state=config["random_state"], cv_splits=config["cv_splits"],
        data_path=str(data_path), final_test_ratio=config["final_test_ratio"], artifact_root=str(work),
        region_id="offline", dataset_id="e1", selected_models=TREES, shap_config=config["shap"],
        feature_selection=None, training_run_id="e1", explain_all_models=False,
    )
    pipeline_seconds = time.perf_counter() - started
    # Same steps as run_pipeline, to score every tree (not only the winner) on the final test.
    df = preprocess(load_observed_target_data(str(data_path)))
    df = df[(df["date_time"] >= config["train_start_date"]) & (df["date_time"] < config["train_end_date"])].copy()
    train_part, val_part, test_part = split_data(df, final_test_ratio=config["final_test_ratio"])
    development = pd.concat([train_part, val_part], ignore_index=True).sort_values("date_time")
    dropped = ["traffic_volume", "date_time"]
    X_dev, y_dev = development.drop(dropped, axis=1), development["traffic_volume"]
    X_test, y_test = test_part.drop(dropped, axis=1), test_part["traffic_volume"]
    comparison = {item["model_name"]: item for item in info["model_comparison"]}
    results = {}
    for name, trainer in TRAINERS.items():
        model = trainer(X_dev, y_dev, config["random_state"])
        predictions = model.predict(X_test)
        test_mae, test_rmse, _ = evaluate(y_test, predictions)
        save_predictions(out / f"{name}_final_test_predictions.csv", test_part["date_time"], y_test, predictions)
        item = comparison[name]
        results[name] = {
            "path": "system: src.pipeline.run_pipeline (legacy_sklearn, weather + calendar features)",
            "cv_MAE_mean": item["validation_MAE"], "cv_MAE_std": item["validation_MAE_std"],
            "cv_RMSE_mean": item["validation_RMSE"], "pipeline_test_MAE": item["test_MAE"],
            "test_MAE": round(float(test_mae), 4), "test_RMSE": round(float(test_rmse), 4),
            "selected_by_pipeline": bool(item.get("selected")), "input_features": list(X_dev.columns),
            "development": [str(development["date_time"].min()), str(development["date_time"].max()), len(development)],
            "final_test": [str(test_part["date_time"].min()), str(test_part["date_time"].max()), len(test_part)],
        }
    results["_pipeline_seconds"] = round(pipeline_seconds, 1)
    return results


def region_variant_results(candidates, path_label, out, suffix=""):
    results = {}
    for candidate in candidates:
        report = json.loads(Path(candidate["source_report"]).read_text(encoding="utf-8"))
        name = candidate["best_model_name"] + suffix
        shutil.copyfile(report["predictions_path"], out / f"{name}_final_test_predictions.csv")
        results[name] = {
            "path": path_label,
            "cv_MAE_mean": report["cv_metrics"]["MAE"]["mean"], "cv_MAE_std": report["cv_metrics"]["MAE"]["std"],
            "cv_RMSE_mean": report["cv_metrics"]["RMSE"]["mean"],
            "test_MAE": report["final_test_metrics"]["MAE"], "test_RMSE": report["final_test_metrics"]["RMSE"],
            "folds": [fold["validation_metrics"] for fold in report["folds"]],
            "development": report["development"], "final_test": report["final_test"],
        }
    return results


def main(name, data_path, train_start, train_end, work_dir, *options):
    patches = []
    if "--patch-median-round" in options:
        apply_median_round_patch()
        patches.append("median-round: build_causal_target_series rounds the rolling-median gap fill "
                       "(experiment-only stand-in for a pending fix; project code unchanged)")
    data_path = Path(data_path).resolve()
    out = ROOT / "experiments" / "outputs" / "e1" / name
    out.mkdir(parents=True, exist_ok=True)
    work = Path(work_dir).resolve() / name
    work.mkdir(parents=True, exist_ok=True)
    os.chdir(work)  # run_pipeline writes data_versions/ and model files relative to the working directory
    config = system_config(train_start, train_end)
    summary = {"name": name, "data_path": str(data_path.relative_to(ROOT)), "config": config,
               "versions": versions(), "patches": patches, "seconds": {}, "models": {}}

    def save():
        (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    started = time.perf_counter()
    summary["models"].update(system_trees(data_path, config, work / "system", out))
    summary["seconds"]["system_trees"] = round(time.perf_counter() - started, 1)
    save()
    print(f"[{name}] system trees done in {summary['seconds']['system_trees']:.0f}s", flush=True)

    common = dict(data_path=str(data_path), artifact_root=str(work / "region"), region_id="offline",
                  dataset_id=name, training_run_id="e1", config=config, region_name="offline")
    for model in ("lstm", "gru"):
        started = time.perf_counter()
        branch = train_region_recurrent_candidate(MODELS, model, **common)
        summary["models"].update(region_variant_results(
            branch["candidates"], "system: train_region_recurrent_candidate", out))
        summary["seconds"][model] = round(time.perf_counter() - started, 1)
        save()
        print(f"[{name}] {model} done in {summary['seconds'][model]:.0f}s", flush=True)

    started = time.perf_counter()
    lag = train_region_tree_lag_candidates(MODELS, **common)
    summary["models"].update(region_variant_results(
        lag["candidates"], "not used by the website: train_region_tree_lag_candidates (lag/rolling features)",
        out, suffix=" (lag)"))
    summary["seconds"]["lag_trees"] = round(time.perf_counter() - started, 1)
    save()
    print(f"[{name}] lag trees done in {summary['seconds']['lag_trees']:.0f}s", flush=True)

    # Model files (hundreds of MB each) are not needed after the predictions are saved.
    os.chdir(ROOT)
    shutil.rmtree(work, ignore_errors=True)
    print(f"[{name}] removed work files in {work}", flush=True)

    for model, item in summary["models"].items():
        if not model.startswith("_"):
            print(f"[{name}] {model:22s} CV MAE {item['cv_MAE_mean']}  test MAE {item['test_MAE']}"
                  f"{'  (pipeline test ' + str(item['pipeline_test_MAE']) + ')' if item.get('pipeline_test_MAE') else ''}",
                  flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
