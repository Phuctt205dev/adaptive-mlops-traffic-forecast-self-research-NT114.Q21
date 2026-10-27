import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src.shap_reporting import explain_saved_model, file_sha256, resolve_shap_config


class ShapReportingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.X = pd.DataFrame({"hour": np.arange(12), "temperature": np.arange(12) % 3})
        self.y = 100 + 10 * self.X.hour

    def explain(self, model, inputs=None, **kwargs):
        path = self.root / "model.pkl"
        joblib.dump(model, path)
        return explain_saved_model(
            model, self.X if inputs is None else inputs, model_name="test",
            model_path=path, output_path=self.root / "shap.json", **kwargs,
        )

    def read_report(self):
        return json.loads((self.root / "shap.json").read_text(encoding="utf-8"))

    def test_disabled_does_not_import_shap_or_write_a_report(self):
        with patch.dict(sys.modules, {"shap": None}):
            result = self.explain(None, config={"enabled": False})
        self.assertEqual(result, {"status": "disabled"})
        self.assertFalse((self.root / "shap.json").exists())

    def test_rejects_invalid_or_unbounded_options(self):
        for options in ({"max_samples": 0}, {"max_samples": 129}, {"nsamples": 100000},
                        {"enabled": "yes"}, {"batch_size": True}, {"unknown": 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                resolve_shap_config(options)

    def test_missing_dependency_has_explicit_persisted_status(self):
        with patch.dict(sys.modules, {"shap": None}):
            result = self.explain(None)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.read_report()["error_type"], "ModuleNotFoundError")

    def test_three_real_tree_algorithms_on_tiny_synthetic_data(self):
        from xgboost import XGBRegressor
        from lightgbm import LGBMRegressor

        models = [RandomForestRegressor(n_estimators=3, max_depth=2, random_state=42),
                  XGBRegressor(n_estimators=3, max_depth=2, n_jobs=1, random_state=42),
                  LGBMRegressor(n_estimators=3, max_depth=2, n_jobs=1,
                                min_child_samples=1, verbosity=-1, random_state=42)]
        for model in models:
            with self.subTest(model=type(model).__name__):
                model.fit(self.X, self.y)
                expected = model.predict(self.X)
                result = self.explain(model, config={"max_samples": 3, "batch_size": 2},
                                      timestamps=pd.Series(pd.date_range("2025-01-01", periods=12, freq="h")))
                self.assertEqual(result["status"], "completed")
                report = self.read_report()
                self.assertEqual(report["sample_indices"], [0, 5, 11])
                self.assertEqual(report["model_sha256"], file_sha256(self.root / "model.pkl"))
                self.assertEqual(report["importance"][0]["feature"], "hour")
                self.assertAlmostEqual(sum(row["share_percent"] for row in report["importance"]), 100)
                self.assertFalse(report["final_test_used"])
                self.assertFalse(report["usable_for_feature_selection"])
                np.testing.assert_allclose(expected, model.predict(self.X))

    def test_uses_fitted_pipeline_transform_without_refitting(self):
        model = Pipeline([("preprocessor", StandardScaler().set_output(transform="pandas")),
                          ("model", RandomForestRegressor(n_estimators=2, random_state=42))])
        model.fit(self.X, self.y)
        path = self.root / "model.pkl"
        joblib.dump(model, path)
        with patch.object(model.named_steps["preprocessor"], "fit", side_effect=AssertionError("Refit")):
            result = explain_saved_model(model, self.X, model_name="test", model_path=path,
                                         output_path=self.root / "shap.json")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.read_report()["feature_names"], list(self.X.columns))

    def test_wrong_feature_order_fails_without_discarding_model(self):
        model = RandomForestRegressor(n_estimators=2, random_state=42).fit(self.X, self.y)
        result = self.explain(model, inputs=self.X[["temperature", "hour"]])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("importance", self.read_report())
        self.assertTrue((self.root / "model.pkl").exists())

    def test_zero_importance_is_finite_and_not_accuracy(self):
        model = RandomForestRegressor(n_estimators=2, random_state=42).fit(self.X, np.ones(12))
        self.assertEqual(self.explain(model)["status"], "completed")
        self.assertEqual(sum(row["share_percent"] for row in self.read_report()["importance"]), 0)

    def test_sequence_sums_time_contributions_and_restores_target_units(self):
        inputs = np.ones((5, 3, 2), dtype=np.float32)
        model = SimpleNamespace(input_shape=(None, 3, 2))
        gradient = Mock()
        def values(batch, **kwargs):
            np.random.seed(123)
            return np.ones((*batch.shape, 1))
        gradient.shap_values.side_effect = values
        fake_shap = SimpleNamespace(__version__="test", GradientExplainer=Mock(return_value=gradient))
        state = np.random.get_state()
        with patch.dict(sys.modules, {"shap": fake_shap}):
            result = self.explain(model, inputs=inputs, sequence=True, target_scale=10,
                                  feature_names=["temperature", "humidity"],
                                  config={"max_samples": 3, "background_samples": 2, "batch_size": 2})
        self.assertEqual(result["status"], "completed")
        report = self.read_report()
        self.assertEqual([row["mean_abs_shap"] for row in report["importance"]], [30, 30])
        self.assertEqual([row["lag_hours"] for row in report["time_importance"]], [3, 2, 1])
        self.assertEqual([row["mean_abs_shap"] for row in report["time_importance"]], [20, 20, 20])
        self.assertEqual(gradient.shap_values.call_count, 2)
        np.testing.assert_array_equal(state[1], np.random.get_state()[1])

    def test_invalid_sequence_values_are_reported_as_failure(self):
        model = SimpleNamespace(input_shape=(None, 3, 2))
        fake_shap = SimpleNamespace(__version__="test", GradientExplainer=Mock(
            return_value=SimpleNamespace(shap_values=lambda *args, **kwargs: np.full((1, 3, 2), np.nan))))
        with patch.dict(sys.modules, {"shap": fake_shap}):
            result = self.explain(model, inputs=np.ones((1, 3, 2)), sequence=True,
                                  feature_names=["a", "b"])
        self.assertEqual(result["status"], "failed")

    def test_time_budget_returns_failure_instead_of_partial_importance(self):
        model = RandomForestRegressor(n_estimators=2, random_state=42).fit(self.X, self.y)
        with patch("src.shap_reporting.time.perf_counter", side_effect=[0.0, 100.0, 101.0]):
            result = self.explain(model)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "TimeoutError")
        self.assertNotIn("importance", self.read_report())

    def test_large_gradient_workload_is_rejected_before_explainer_creation(self):
        fake_shap = SimpleNamespace(__version__="test", GradientExplainer=Mock())
        model = SimpleNamespace(input_shape=(None, 512, 512))
        with patch.dict(sys.modules, {"shap": fake_shap}):
            result = self.explain(model, inputs=np.zeros((1, 512, 512), dtype=np.float32),
                                  feature_names=[f"f{i}" for i in range(512)], sequence=True)
        self.assertEqual(result["error_type"], "MemoryError")
        fake_shap.GradientExplainer.assert_not_called()

    def test_report_write_failure_does_not_remove_candidate_or_leave_temp_file(self):
        model = RandomForestRegressor(n_estimators=2, random_state=42).fit(self.X, self.y)
        with patch.object(Path, "replace", side_effect=OSError("full")):
            result = self.explain(model)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("source_report", result)
        self.assertTrue((self.root / "model.pkl").exists())
        self.assertEqual(list(self.root.glob("*.tmp")), [])


@unittest.skipUnless(importlib.util.find_spec("tensorflow"), "TensorFlow is not installed locally")
class NeuralShapCompatibilityTests(unittest.TestCase):
    def test_lstm_and_gru_can_be_explained_without_training(self):
        from tensorflow import keras
        from src.models.recurrent import build_recurrent_model
        X = np.random.default_rng(42).normal(size=(4, 3, 2)).astype(np.float32)
        for name in ("LSTM", "GRU"):
            with self.subTest(model=name), tempfile.TemporaryDirectory() as directory:
                keras.backend.clear_session()
                model = build_recurrent_model(name, (3, 2), recurrent_units=2, dense_units=2)
                model_path = Path(directory) / "model.keras"
                model.save(model_path)
                result = explain_saved_model(model, X, model_name=name, model_path=model_path,
                    output_path=Path(directory) / "shap.json", sequence=True, feature_names=["a", "b"],
                    config={"max_samples": 2, "background_samples": 2, "batch_size": 1, "nsamples": 16})
                self.assertEqual(result["status"], "completed")
                keras.backend.clear_session()


if __name__ == "__main__":
    unittest.main()
