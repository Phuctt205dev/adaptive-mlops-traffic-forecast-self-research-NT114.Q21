import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from backend.app.schemas.model_version import TrainingRunCreate
from backend.app.services.drift_monitoring import _training_configuration
from src import time_series_cross_validation as cv
from src.time_series_training import build_preprocessor
from tests.test_time_series_training import make_feature_data


class ShapPipelineTests(unittest.TestCase):
    def test_cv_report_transports_shap_and_excludes_final_test_samples(self):
        def tiny_pipeline(model_name, feature_columns, random_state):
            return Pipeline([("preprocessor", build_preprocessor(feature_columns)),
                             ("model", RandomForestRegressor(n_estimators=3, random_state=random_state))])
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cv, "build_training_pipeline", tiny_pipeline), patch.object(cv, "_load_mlflow", return_value=None):
                report = cv.run_model_time_series_cross_validation(
                    "RandomForest", make_feature_data(), n_splits=2,
                    output_directory=str(Path(directory) / "models"),
                    result_directory=str(Path(directory) / "results"),
                    train_start_date="2024-01-01", train_end_date="2024-01-05",
                    shap_config={"max_samples": 3},
                    shap_context={"region_id": "test-region", "dataset_id": "test-dataset"},
                )
            self.assertEqual(report["shap_report"]["status"], "completed")
            path = report["shap_report"]["source_report"]
            self.assertIn(path, report["artifact_paths"])
            explanation = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(explanation["context"]["dataset_id"], "test-dataset")
            self.assertLess(max(pd.to_datetime(explanation["sample_target_timestamps"])),
                            pd.Timestamp(report["final_test"]["start"]))

    def test_final_neural_hook_uses_training_sequences_and_saved_preprocessors(self):
        # No TensorFlow, fitting, dataset download or external services in this test.
        X_train = np.ones((6, 3, 2), dtype=np.float32)
        datasets = {
            "train": {"X": X_train, "y": np.ones((6, 1)),
                      "timestamps": pd.date_range("2025-01-01", periods=6, freq="h").to_numpy()},
            "validation": {"X": np.full((2, 3, 2), 999.0), "y": np.ones((2, 1)),
                           "raw_y": np.array([100.0, 200.0]),
                           "timestamps": pd.date_range("2025-01-02", periods=2, freq="h").to_numpy()},
        }
        scaler = StandardScaler().fit(np.array([[100.0], [200.0]]))
        preprocessors = {"sequence_feature_names": ["hour", "temperature"], "target_scaler": scaler}
        model = Mock()
        model.predict.return_value = np.zeros((2, 1))
        model.save.side_effect = lambda path: Path(path).write_bytes(b"test model")
        fake_modules = {
            "tensorflow": SimpleNamespace(keras=SimpleNamespace(backend=SimpleNamespace(clear_session=Mock()))),
            "src.models.recurrent": SimpleNamespace(build_recurrent_model=Mock(return_value=model)),
            "src.neural_utils": SimpleNamespace(configure_reproducibility=Mock(),
                inverse_scale_predictions=lambda values, target: target.inverse_transform(values).reshape(-1)),
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, fake_modules):
            for name in ("LSTM", "GRU"):
                with self.subTest(model=name), patch.object(cv, "_build_neural_fold_datasets", return_value=(datasets, preprocessors)), \
                        patch.object(cv, "explain_saved_model", return_value={"status": "completed"}) as explain:
                    result = cv._train_final_neural_model(
                        name, None, pd.DataFrame({"date_time": [1]}), pd.DataFrame({"date_time": [2]}),
                        3, 1, 2, 42, str(Path(directory) / f"{name}.keras"),
                        str(Path(directory) / f"{name}_preprocessor.pkl"),
                        str(Path(directory) / f"{name}_predictions.csv"), 0,
                    )
                    self.assertEqual(result["shap_report"]["status"], "completed")
                    self.assertIs(explain.call_args.args[1], X_train)
                    self.assertEqual(explain.call_args.kwargs["feature_names"], ["hour", "temperature"])
                    self.assertEqual(explain.call_args.kwargs["target_scale"], 50.0)
                    self.assertTrue(explain.call_args.kwargs["sequence"])
                    self.assertIn("preprocessor_sha256", explain.call_args.kwargs["context"])

    def test_training_request_can_disable_shap_and_reject_excessive_work(self):
        request = TrainingRunCreate(train_start_date="2025-01-01", train_end_date="2025-02-01",
                                    shap={"enabled": False})
        self.assertFalse(request.model_dump()["shap"]["enabled"])
        with self.assertRaises(ValueError):
            TrainingRunCreate(train_start_date="2025-01-01", train_end_date="2025-02-01",
                              shap={"max_samples": 10000})

    def test_drift_retraining_preserves_shap_options(self):
        previous = {"shap": {"enabled": False}, "selected_models": ["lstm"]}
        result = _training_configuration(SimpleNamespace(configuration_json=previous), pd.Timestamp("2025-02-01"))
        self.assertEqual(result["shap"], {"enabled": False})


if __name__ == "__main__":
    unittest.main()
