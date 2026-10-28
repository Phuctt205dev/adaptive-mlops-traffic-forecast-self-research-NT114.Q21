"""Publish valid controlled output to central MLflow, with scoped rollback."""
import copy
import math
from pathlib import Path
import re
from urllib.parse import urlparse

from src.mlflow_regions import region_experiment_name


class TrainingTracker:
    def __init__(self, client=None, repository_factory=None):
        if client is None:
            from mlflow import MlflowClient
            client = MlflowClient()
        self.client = client
        if repository_factory is None:
            from mlflow.store.artifact.artifact_repository_registry import get_artifact_repository
            repository_factory = get_artifact_repository
        self.repository_factory = repository_factory

    def experiment(self, run):
        name = region_experiment_name(region_id=run.region_id)
        experiment = self.client.get_experiment_by_name(name)
        if experiment is None:
            identity = self.client.create_experiment(name, tags={"traffic_region_id": str(run.region_id)})
        else:
            identity = experiment.experiment_id
            if experiment.lifecycle_stage == "deleted":
                self.client.restore_experiment(identity)
            self.client.set_experiment_tag(identity, "traffic_region_id", str(run.region_id))
        return identity

    def owned_runs(self, run, experiment_id):
        return self.client.search_runs([experiment_id],
            filter_string=f"tags.traffic_training_run_id = '{run.id}'", run_view_type=3, max_results=100)  # ViewType.ALL

    def publish(self, run, info):
        experiment_id = self.experiment(run)
        existing = self.owned_runs(run, experiment_id)
        active = [entry for entry in existing if entry.info.lifecycle_stage == "active"]
        if len(active) > 1:
            raise RuntimeError("Multiple central tracking runs require recovery.")
        entry = active[0] if active else self.client.create_run(experiment_id,
            tags={"traffic_training_run_id": str(run.id), "traffic_region_id": str(run.region_id),
                  "traffic_dataset_id": str(run.dataset_id), "training_backend": run.configuration_json["training_backend"]})
        run_id = entry.info.run_id
        for name in ("validation_MAE", "validation_MAE_std", "test_MAE", "test_RMSE", "test_MAPE"):
            value = info.get(name)
            if value is not None:
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError("Invalid central training metric.")
                self.client.log_metric(run_id, name, float(value))
        for key in ("cv_splits", "random_state", "best_model_name", "model_family"):
            if info.get(key) is not None:
                self.client.log_param(run_id, key, str(info[key]))
        files = {}
        for key in ("model_file", "preprocessor_file", "compatibility_file", "source_report"):
            if info.get(key):
                files[key] = Path(info[key])
        report = (info.get("shap_report") or {}).get("source_report")
        if report:
            files["shap_report"] = Path(report)
        for key, path in files.items():
            self.client.log_artifact(run_id, str(path), artifact_path=key)
        self.client.set_terminated(run_id, status="FINISHED")
        result = copy.deepcopy(info)
        result.update(mlflow_run_id=run_id, mlflow_model_uri=f"runs:/{run_id}/model_file/{files['model_file'].name}")
        return result

    def cleanup(self, run):
        """Only failed/uncommitted run output; caller fences registry first."""
        experiment = self.client.get_experiment_by_name(region_experiment_name(region_id=run.region_id))
        if experiment is None:
            return True
        experiment_id = experiment.experiment_id
        for entry in self.owned_runs(run, experiment_id):
            tags, run_id = entry.data.tags, entry.info.run_id
            if (tags.get("traffic_training_run_id") != str(run.id)
                    or tags.get("traffic_region_id") != str(run.region_id)
                    or not re.fullmatch("[a-f0-9]{32}", run_id)):
                raise RuntimeError("Central tracking cleanup ownership differs.")
            uri = entry.info.artifact_uri
            if run_id not in urlparse(uri).path.split("/"):
                raise RuntimeError("Tracking artifact prefix is not owned by this run.")
            self.repository_factory(uri).delete_artifacts()
            if self.client.list_artifacts(run_id):
                return False
            if entry.info.lifecycle_stage != "deleted":
                self.client.delete_run(run_id)
        return True
