"""All selected trees produce final SHAP JSON while only winning weights persist."""
from contextlib import ExitStack, nullcontext
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
import numpy as np
import pandas as pd

from src import pipeline
from src.cloud_training.artifacts import write_bundle, read_bundle
from src.region_training_variants import _candidate_from_report
from tests.test_region_pipeline import make_raw_training_csv, train_constant


class AllModelShapTests(unittest.TestCase):
    def test_real_tiny_tree_fits_produce_three_final_reports(self):
        from sklearn.ensemble import RandomForestRegressor
        from xgboost import XGBRegressor
        from lightgbm import LGBMRegressor
        def trainer(cls):
            def fit(X, y, seed):
                kwargs = {"n_estimators": 3, "max_depth": 2, "random_state": seed, "n_jobs": 1}
                if cls is LGBMRegressor:
                    kwargs.update(min_child_samples=2, verbosity=-1)
                return cls(**kwargs).fit(X, y)
            return fit
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            path = root / "synthetic.csv"
            make_raw_training_csv(path)
            frame = pd.read_csv(path)
            frame["temperature"] = np.arange(len(frame), dtype=float)
            frame["traffic_volume"] = 100 + frame["temperature"] * 10
            frame.to_csv(path, index=False)
            stack.enter_context(patch.object(pipeline, "mlflow", pipeline._NullMLflow()))
            for name, cls in (("train_random_forest", RandomForestRegressor), ("train_xgboost", XGBRegressor), ("train_lightgbm", LGBMRegressor)):
                stack.enter_context(patch.object(pipeline, name, side_effect=trainer(cls)))
            info = pipeline.run_pipeline("2025-01-01", "2025-01-03", data_path=str(path), data_audit_path="",
                artifact_root=str(root), region_id="synthetic-region", dataset_id="synthetic-dataset",
                training_run_id="synthetic-run", cv_splits=2, explain_all_models=True, shap_config={"max_samples": 2})
            self.assertEqual(set(info["shap_reports"]), {"RandomForest", "XGBoost", "LightGBM"})
            for name, metadata in info["shap_reports"].items():
                self.assertEqual(metadata["status"], "completed", name)
                report = json.loads(Path(metadata["source_report"]).read_text(encoding="utf-8"))
                self.assertFalse(report["final_test_used"])
                self.assertTrue(report["importance"])
                self.assertEqual(report["context"]["training_run_id"], "synthetic-run")

    def test_tree_reports_share_development_context_and_transport_without_extra_weights(self):
        calls = []
        run_id, region_id, dataset_id = (str(uuid.uuid4()) for _ in range(3))
        def explain(model, inputs, **kwargs):
            path = Path(kwargs["output_path"])
            raw = json.dumps({"context": kwargs["context"], "importance": []}).encode()
            path.write_bytes(raw)
            calls.append((kwargs["model_name"], len(inputs), kwargs["timestamps"].max(), kwargs["model_path"]))
            return {"status": "completed", "source_report": str(path), "report_sha256": hashlib.sha256(raw).hexdigest(),
                    "model_sha256": hashlib.sha256(Path(kwargs["model_path"]).read_bytes()).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            data = root / "synthetic.csv"
            make_raw_training_csv(data)
            for name in ("train_random_forest", "train_xgboost", "train_lightgbm"):
                stack.enter_context(patch.object(pipeline, name, side_effect=train_constant))
            stack.enter_context(patch.object(pipeline, "mlflow", pipeline._NullMLflow()))
            stack.enter_context(patch.object(pipeline, "explain_saved_model", side_effect=explain))
            info = pipeline.run_pipeline("2025-01-01", "2025-01-03", data_path=str(data), data_audit_path="",
                artifact_root=str(root), region_id=region_id, dataset_id=dataset_id, training_run_id=run_id,
                selected_models=["random_forest_lag", "xgboost_lag", "lightgbm_lag"],
                cv_splits=2, explain_all_models=True)
            self.assertEqual(set(info["shap_reports"]), {"RandomForest", "XGBoost", "LightGBM"})
            self.assertEqual({call[1] for call in calls}, {40})
            self.assertEqual(len(calls), 3)
            for name, _, end, model_path in calls:
                self.assertLess(str(end), "2025-01-02 16:00:00")
                metadata = info["shap_reports"][name]
                report = json.loads(Path(metadata["source_report"]).read_bytes())
                self.assertEqual(report["context"]["training_run_id"], run_id)
                if name != info["best_model_name"]:
                    self.assertFalse(Path(model_path).exists())
            # Every report crosses the existing path/hash-bound cloud contract.
            context = {"probe": "synthetic"}
            manifest = write_bundle({"candidates": [info]}, context, root)
            restored = read_bundle(root, context)["candidates"][0]
            self.assertEqual(set(restored["shap_reports"]), set(info["shap_reports"]))
            self.assertEqual(len([f for f in manifest["files"] if f["path"].endswith(".pkl")]), 2)

    def test_neural_candidate_keeps_report_sequence_and_preprocessor_per_algorithm(self):
        for name in ("LSTM", "GRU"):
            report = {"variant": name.lower(), "model": name, "family": "neural_sequence",
                "split_policy": {"cv_splits": 2}, "production_reserved": {}, "cv_metrics": {
                    "MAE": {"mean": 1, "std": 0}, "RMSE": {"mean": 1}, "MAPE": {"mean": 1}},
                "final_test_metrics": {"MAE": 1, "RMSE": 1, "MAPE": 1}, "random_state": 42,
                "model_path": "model.keras", "preprocessor_path": "pre.pkl", "artifact_paths": [],
                "report_path": "metrics.json", "training_configuration": {"sequence_length_hours": 72},
                "model_input_features": ["temperature", "weather_type_Rain"],
                "shap_report": {"status": "completed", "source_report": "shap.json", "preprocessor_sha256": "b" * 64}}
            candidate = _candidate_from_report(report, "region", "dataset", "synthetic.csv", "candidate")
            metadata = candidate["shap_reports"][name]
            self.assertEqual(metadata["sequence_length"], 72)
            self.assertEqual(metadata["preprocessor_file"], "pre.pkl")
            self.assertEqual(metadata["input_features"], ["temperature", "weather_type_Rain"])
