"""Execute one pure ML branch in a caller-owned process and staging directory.

The parent controls process lifetime. This module never imports a backend,
opens a database, publishes to S3, registers a model or changes the active model.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


def guard_parent():
    """Linux worker death must not leave a detached ML child fitting."""
    expected = os.environ.get("TRAFFIC_EXECUTION_PARENT_PID")
    if expected and sys.platform == "linux":
        import ctypes
        import signal
        if not expected.isdigit() or ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise RuntimeError("The execution parent guard could not be installed.")
        if os.getppid() != int(expected):
            raise RuntimeError("The execution controller exited before ML startup.")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.cloud_training.artifacts import write_bundle
from src.cloud_training.probe import canonical, sha256_file
from src.cloud_training.package import configuration_digest
from src.training_progress import reporting, report, set_fold
from src.cloud_training.errors import FAILURE_PREFIX, safe_failure

PREFIX = "TRAFFIC_EXECUTION_EVENT "


def prepare_runtime(job):
    """Resolve only the reviewed dependency location/profile, before ML imports."""
    config = job["configuration"]
    dependencies = Path("/opt/traffic-shap-deps")
    if config.get("training_backend", "local") == "local" and dependencies.is_dir():
        sys.path.insert(0, str(dependencies))
    if config.get("training_backend") == "kaggle" and job.get("runtime_profile"):
        import importlib.metadata
        import re
        import subprocess
        import tempfile
        profile = job["runtime_profile"]
        if (set(profile) - {"scikit-learn", "joblib", "xgboost", "lightgbm", "pandas", "shap"}
                or any(not isinstance(v, str) or not re.fullmatch(r"[0-9][a-zA-Z0-9.+-]{0,50}", v) for v in profile.values())):
            raise ValueError("Invalid runtime dependency profile.")
        missing = []
        for name, version in profile.items():
            try:
                installed = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                installed = None
            if installed != version:
                missing.append(f"{name}=={version}")
        if missing:
            target = tempfile.mkdtemp(prefix="traffic-ml-dependencies-")
            subprocess.run([sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
                            "--no-deps", "--target", target, *missing], check=True, timeout=180,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sys.path.insert(0, target)
            importlib.invalidate_caches()


def run(job, dataset, output):
    import time
    started = time.perf_counter()
    context, config = job["context"], job["configuration"]
    if (sha256_file(dataset) != job["dataset_sha256"]
            or configuration_digest(config) != context["configuration_sha256"]):
        raise ValueError("Dataset/configuration hash differs from the execution contract.")
    prepare_runtime(job)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["MLFLOW_TRACKING_URI"] = (output / "offline-mlflow").as_uri()
    os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"
    branch, selected = context["branch"], config["selected_models"]
    if config.get("training_backend") == "kaggle" and branch in {"lstm", "gru"} and branch in selected:
        import tensorflow as tf
        if not tf.config.list_physical_devices("GPU"):
            raise RuntimeError("The selected cloud GPU execution has no usable GPU.")

    def progress(event):
        print(PREFIX + json.dumps({"context": context, "event": event}, allow_nan=False), flush=True)

    with reporting(progress):
        if branch == "tree":
            if not any(model.endswith("_lag") for model in selected):
                result = {"branch": "tree", "status": "skipped", "candidates": []}
            else:
                from src.pipeline import run_pipeline
                info = run_pipeline(
                    train_start_date=config["train_start_date"], train_end_date=config["train_end_date"],
                    model_role="candidate", random_state=int(config.get("random_state", 42)),
                    cv_splits=int(config.get("cv_splits", 3)), data_path=str(dataset),
                    artifact_root=str(output), region_id=context["region_id"], dataset_id=context["dataset_id"],
                    mlflow_tracking_uri=os.environ["MLFLOW_TRACKING_URI"], selected_models=selected,
                    shap_config=config.get("shap"),
                    feature_selection=config.get("feature_selection"),
                    training_run_id=context["run_id"], explain_all_models=True,
                )
                info.update(model_family="legacy_sklearn", benchmark_only=False, inference_supported=True,
                            selection_metric="cross_validation_mean_MAE")
                result = {"branch": "tree", "status": "completed", "production_candidate": info, "candidates": [info]}
        elif branch in {"lstm", "gru"}:
            from src.region_training_variants import train_region_recurrent_candidate
            if branch in selected:
                report(branch, "model_started")
            result = train_region_recurrent_candidate(
                selected_models=selected, model_name=branch.upper(), data_path=str(dataset),
                artifact_root=str(output), region_id=context["region_id"], dataset_id=context["dataset_id"],
                training_run_id=context["run_id"], config=config,
            )
            for item in result.get("candidates", []):
                item.update(benchmark_only=False, inference_supported=True)
                if config.get("shap", {}).get("enabled", True) and item.get("shap_report", {}).get("status") != "completed":
                    raise RuntimeError("The requested neural SHAP report did not complete.")
            if branch in selected:
                set_fold()
                report(branch, "model_evaluated")
        else:
            raise ValueError("Unsupported training branch.")
    cost = {"execution_seconds": round(time.perf_counter() - started, 4),
            "includes": "preparation_cv_selection_final_fit_test_and_shap"}
    try:
        import resource
        cost["process_peak_rss_bytes"] = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
        cost["memory_scope"] = "process_high_water_mark_linux"
    except ImportError:
        pass
    result["execution_cost"] = cost
    for candidate in result.get("candidates", []):
        candidate["execution_cost"] = cost
    write_bundle(result, context, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    job = None
    try:
        guard_parent()
        job = json.loads(Path(args.job).read_text(encoding="utf-8"))
        run(job, Path(args.dataset), Path(args.output))
        return 0
    except Exception as error:
        # Raw exception strings can contain connection URLs and dataset values.
        if job and isinstance(job.get("context"), dict):
            print(FAILURE_PREFIX + json.dumps({"context": job["context"], "failure": safe_failure(error)}), flush=True)
        else:
            print(json.dumps({"execution_failed": True, "error_type": type(error).__name__}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
