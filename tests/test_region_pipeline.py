import os
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from unittest.mock import patch

import numpy as np
import pandas as pd

from scripts.training.run_region_pipeline import parse_s3_uri
from src.pipeline import run_pipeline


class ConstantModel:
    def fit(self, _features, _target):
        return self

    def predict(self, features):
        return np.full(len(features), 100.0)


def make_raw_training_csv(path, rows=48):
    frame = pd.DataFrame(
        {
            "date_time": pd.date_range("2025-01-01", periods=rows, freq="h"),
            "is_holiday": ["None"] * rows,
            "air_pollution_index": [42] * rows,
            "humidity": [85] * rows,
            "wind_speed": [3] * rows,
            "wind_direction": [180] * rows,
            "visibility_in_miles": [10] * rows,
            "dew_point": [12] * rows,
            "temperature": [18.5] * rows,
            "rain_p_h": [0.0] * rows,
            "snow_p_h": [0.0] * rows,
            "clouds_all": [20] * rows,
            "weather_type": ["Clear"] * rows,
            "weather_description": ["clear sky"] * rows,
            "traffic_volume": [100] * rows,
        }
    )
    frame.to_csv(path, index=False)


def train_constant(_features, _target, _random_state):
    return ConstantModel()


class RegionPipelineTests(unittest.TestCase):
    def test_s3_uri_parser(self):
        bucket, key = parse_s3_uri("s3://traffic-datasets/a/b/file.csv")

        self.assertEqual(bucket, "traffic-datasets")
        self.assertEqual(key, "a/b/file.csv")

    def test_run_pipeline_writes_region_scoped_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            data_path = os.path.join(directory, "region.csv")
            artifact_root = os.path.join(directory, "artifacts")
            make_raw_training_csv(data_path)

            patches = [
                patch("src.pipeline.train_random_forest", train_constant),
                patch("src.pipeline.train_xgboost", train_constant),
                patch("src.pipeline.train_lightgbm", train_constant),
                patch("src.pipeline.mlflow.set_tracking_uri"),
                patch("src.pipeline.mlflow.set_experiment"),
                patch("src.pipeline.mlflow.data.from_pandas", return_value=object()),
                patch("src.pipeline.mlflow.start_run", return_value=nullcontext()),
                patch("src.pipeline.mlflow.log_input"),
                patch("src.pipeline.mlflow.log_metrics"),
                patch("src.pipeline.mlflow.log_params"),
                patch("src.pipeline.mlflow.set_tags"),
                patch("src.pipeline.mlflow.log_artifact"),
                patch("src.pipeline.mlflow.sklearn.log_model"),
                patch("src.pipeline.explain_saved_model", return_value={"status": "completed"}),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12], patches[13] as explain:
                model_info = run_pipeline(
                    train_start_date="2025-01-01",
                    train_end_date="2025-01-03",
                    model_role="candidate",
                    cv_splits=2,
                    data_path=data_path,
                    data_audit_path="",
                    region_id="region-a",
                    dataset_id="dataset-a",
                    artifact_root=artifact_root,
                )

            region_root = os.path.join(artifact_root, "regions", "region-a")
            self.assertEqual(model_info["region_id"], "region-a")
            self.assertEqual(model_info["dataset_id"], "dataset-a")
            self.assertEqual(model_info["shap_report"]["status"], "completed")
            explain.assert_called_once()
            # Shared rounding reserves 7 of 48 rows for the default 15% test.
            self.assertEqual(len(explain.call_args.args[1]), 41)
            self.assertNotIn("traffic_volume", explain.call_args.args[1].columns)
            self.assertEqual(explain.call_args.kwargs["timestamps"].max(), pd.Timestamp("2025-01-02 16:00"))
            self.assertTrue(
                model_info["model_file"].startswith(
                    os.path.join(region_root, "models")
                )
            )
            self.assertTrue(os.path.exists(model_info["model_file"]))
            self.assertTrue(
                os.path.exists(
                    os.path.join(region_root, "data_versions", "version_log.csv")
                )
            )

    def test_nondefault_holdout_keeps_final_test_out_of_all_tree_fits(self):
        from src import pipeline
        fitted_rows = []

        def train(features, target, seed):
            self.assertEqual(seed, 17)
            fitted_rows.append(features.index.tolist())
            return train_constant(features, target, seed)

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            data_path = os.path.join(directory, "synthetic.csv")
            make_raw_training_csv(data_path, rows=72)
            stack.enter_context(patch.object(pipeline, "mlflow", pipeline._NullMLflow()))
            for name in ("train_random_forest", "train_xgboost", "train_lightgbm"):
                stack.enter_context(patch.object(pipeline, name, side_effect=train))
            explain = stack.enter_context(patch.object(pipeline, "explain_saved_model", return_value={"status": "disabled"}))
            info = run_pipeline("2025-01-01", "2025-01-04", data_path=data_path, data_audit_path="",
                                artifact_root=directory, cv_splits=4, random_state=17, final_test_ratio=0.25)
            self.assertEqual(info["final_test_ratio"], 0.25)
            self.assertEqual(info["split_policy"]["final_test_start"], "2025-01-03T06:00:00")
            self.assertEqual(len(explain.call_args.args[1]), 54)
            self.assertEqual(len(fitted_rows), 3 * 4 + 1)
            self.assertTrue(all(max(indices) < 54 for indices in fitted_rows))
            self.assertEqual(set(row["model_name"] for row in info["model_comparison"]), {"RandomForest", "XGBoost", "LightGBM"})


if __name__ == "__main__":
    unittest.main()
