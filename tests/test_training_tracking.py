from pathlib import Path
from types import SimpleNamespace as Object
import unittest
import uuid

from backend.app.services.training_tracking import TrainingTracker


class TrackingClient:
    def __init__(self):
        self.experiment, self.runs, self.files, self.metrics = None, [], [], []
    def get_experiment_by_name(self, name):
        return self.experiment
    def create_experiment(self, name, tags):
        self.experiment = Object(experiment_id="9", lifecycle_stage="active")
        return "9"
    def set_experiment_tag(self, *args):
        pass
    def search_runs(self, *args, **kwargs):
        return self.runs
    def create_run(self, experiment_id, tags):
        identity = uuid.uuid4().hex
        run = Object(info=Object(run_id=identity, artifact_uri=f"mlflow-artifacts:/9/{identity}/artifacts",
                                 lifecycle_stage="active"), data=Object(tags=tags))
        self.runs.append(run)
        return run
    def log_metric(self, run_id, name, value):
        self.metrics.append((name, value))
    def log_param(self, *args):
        pass
    def log_artifact(self, run_id, path, artifact_path):
        self.files.append((run_id, path, artifact_path))
    def set_terminated(self, *args, **kwargs):
        pass
    def list_artifacts(self, run_id):
        return [item for item in self.files if item[0] == run_id]
    def delete_run(self, run_id):
        next(item for item in self.runs if item.info.run_id == run_id).info.lifecycle_stage = "deleted"


class TrainingTrackingTests(unittest.TestCase):
    def setUp(self):
        self.run = Object(id=uuid.uuid4(), region_id=uuid.uuid4(), dataset_id=uuid.uuid4(),
                          configuration_json={"training_backend": "kaggle"})
        self.client = TrackingClient()
        self.deleted = []
        def repository(uri):
            def delete():
                self.deleted.append(uri)
                self.client.files.clear()
            return Object(delete_artifacts=delete)
        self.tracker = TrainingTracker(self.client, repository)
        self.info = {"model_file": "/verified/model.pkl", "model_family": "legacy_sklearn", "best_model_name": "RandomForest",
                     "validation_MAE": 2.0, "validation_MAE_std": 1.0, "test_MAE": 3.0}

    def test_valid_metrics_and_artifacts_have_a_central_owned_run(self):
        result = self.tracker.publish(self.run, self.info)
        self.assertTrue(result["mlflow_model_uri"].startswith("runs:/"))
        self.assertEqual(self.client.runs[0].data.tags["training_backend"], "kaggle")
        self.assertEqual(len(self.client.runs), 1)
        self.tracker.publish(self.run, self.info)
        self.assertEqual(len(self.client.runs), 1)

    def test_failed_publication_removes_owned_artifacts_and_deletes_tracking_run(self):
        self.tracker.publish(self.run, self.info)
        self.assertTrue(self.tracker.cleanup(self.run))
        self.assertTrue(self.deleted)
        self.assertEqual(self.client.runs[0].info.lifecycle_stage, "deleted")
        self.assertFalse(self.client.files)

    def test_all_algorithm_reports_are_logged_with_the_single_registered_candidate(self):
        self.info["shap_reports"] = {name: {"source_report": f"/verified/{name}.json"}
                                     for name in ("RandomForest", "XGBoost", "LightGBM", "LSTM", "GRU")}
        result = self.tracker.publish(self.run, self.info)
        paths = {item[2] for item in self.client.files}
        self.assertTrue({"shap_" + name for name in self.info["shap_reports"]} <= paths)
        self.assertEqual(len(self.client.runs), 1)
        self.assertEqual(set(result["shap_reports"]), set(self.info["shap_reports"]))

    def test_cleanup_cannot_delete_foreign_or_unscoped_tracking_output(self):
        self.tracker.publish(self.run, self.info)
        self.client.runs[0].data.tags["traffic_region_id"] = str(uuid.uuid4())
        with self.assertRaises(RuntimeError):
            self.tracker.cleanup(self.run)
        self.assertFalse(self.deleted)

    def test_absent_experiment_requires_no_creation_for_cleanup(self):
        self.assertTrue(self.tracker.cleanup(self.run))
        self.assertIsNone(self.client.experiment)


if __name__ == "__main__":
    unittest.main()
