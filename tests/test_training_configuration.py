"""Configuration reaches ML entrypoints without fitting or external services."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from pydantic import ValidationError

from backend.app.schemas.model_version import TrainingRunCreate
from scripts.cloud_training import execute_branch
from src.cloud_training.package import configuration_digest
from src.cloud_training.probe import sha256_file
from src.preprocess import split_data
from src import region_training_variants as variants
from src.time_series_cross_validation import _split_by_dynamic_window
from src.time_series_training import BASELINE_COLUMNS, CATEGORICAL_COLUMNS


def configuration(**overrides):
    return TrainingRunCreate(**{
        "train_start_date": "2025-01-01", "train_end_date": "2025-02-01",
        "cv_splits": 4, "random_state": 17, "final_test_ratio": 0.25,
        "recurrent_sequence_length": 48, "recurrent_epochs": 7, "recurrent_batch_size": 64,
        **overrides,
    }).model_dump(mode="json")


class SystemManagedFieldTests(unittest.TestCase):
    """artifact_root and model_role are chosen by the system, never by the caller."""

    def test_caller_values_are_replaced_by_the_system_values(self):
        for root, role in (("/etc", "champion"), ("../../outside", ""), ("C:/Windows", "x" * 500)):
            with self.subTest(root=root):
                stored = configuration(artifact_root=root, model_role=role)
                self.assertEqual(stored["artifact_root"], "models/regions")
                self.assertEqual(stored["model_role"], "candidate")

    def test_defaults_and_ui_values_are_unchanged(self):
        stored = configuration(artifact_root="models/regions", model_role="candidate")
        self.assertEqual((stored["artifact_root"], stored["model_role"]), ("models/regions", "candidate"))
        self.assertEqual(configuration()["artifact_root"], "models/regions")

    def test_old_stored_configuration_still_validates_and_compares_equal(self):
        legacy = {**configuration(), "artifact_root": "models/old-layout", "model_role": "champion"}
        current = {**configuration()}
        again = TrainingRunCreate.model_validate(legacy).model_dump(mode="json")
        same_shape = TrainingRunCreate.model_validate(current).model_dump(mode="json")
        self.assertEqual(again, same_shape)


class TrainingConfigurationTests(unittest.TestCase):
    def test_tree_and_neural_reserve_the_same_latest_rows_including_rounding(self):
        for size in (48, 71, 100):
            frame = pd.DataFrame({"date_time": pd.date_range("2025-01-01", periods=size, freq="h"),
                                  "traffic_volume": range(size)})
            for name in CATEGORICAL_COLUMNS:
                frame[name] = "Clear"
            for name in BASELINE_COLUMNS.values():
                frame[name] = 1.0
            for ratio in (0.01, 0.15, 0.25, 0.49):
                with self.subTest(size=size, ratio=ratio):
                    train, validation, tree_test = split_data(frame.iloc[::-1], ratio)
                    _, _, development, neural_test = _split_by_dynamic_window(
                        frame, "2025-01-01", "2025-02-01", ratio)
                    self.assertEqual(tree_test.date_time.tolist(), neural_test.date_time.tolist())
                    self.assertEqual(pd.concat([train, validation]).date_time.tolist(), development.date_time.tolist())
                    self.assertLess(development.date_time.max(), tree_test.date_time.min())

    def test_invalid_values_are_rejected_before_dispatch(self):
        invalid = {"cv_splits": [0, 1, 2.5], "random_state": [-1, 4294967296],
                   "recurrent_sequence_length": [0, -1], "recurrent_epochs": [0, 1.5],
                   "recurrent_batch_size": [0, -1], "final_test_ratio": [0, -0.1, 0.5, float("nan"), float("inf")],
                   "train_start_date": ["2025-02-30", "2025-01-01T00:00:00", "2025-02-01"],
                   "train_end_date": ["2024-12-31", "2025-01-01"]}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                    configuration(**{key: value})

    def test_tree_cloud_entrypoint_forwards_nondefault_holdout_and_cv(self):
        config = configuration()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "synthetic.csv"
            dataset.write_text("synthetic,not used for fitting\n", encoding="utf-8")
            context = {"configuration_sha256": configuration_digest(config), "branch": "tree",
                       "region_id": "region", "dataset_id": "dataset", "run_id": "run"}
            job = {"context": context, "configuration": config, "dataset_sha256": sha256_file(dataset)}
            with patch.object(execute_branch, "prepare_runtime"), patch.object(execute_branch, "write_bundle"), \
                    patch("src.pipeline.run_pipeline", return_value={}) as train, redirect_stdout(io.StringIO()):
                execute_branch.run(job, dataset, root / "output")
            passed = train.call_args.kwargs
            for key in ("train_start_date", "train_end_date", "cv_splits", "random_state", "final_test_ratio"):
                self.assertEqual(passed[key], config[key])

    def test_both_neural_models_receive_the_same_shared_and_recurrent_settings(self):
        config = configuration()
        prepared = {"feature_df": None, "hourly_df": None, "audit_df": None}
        with tempfile.TemporaryDirectory() as temporary:
            for variant in ("lstm", "gru"):
                with self.subTest(variant=variant), \
                        patch.object(variants, "run_model_time_series_cross_validation", return_value={}) as train, \
                        patch.object(variants, "_candidate_from_report", return_value={}):
                    variants._train_variant(variant, prepared, temporary, "region", "dataset", "run", config, "unused.csv")
                    passed = train.call_args.kwargs
                    for key in ("train_start_date", "train_end_date", "random_state", "final_test_ratio"):
                        self.assertEqual(passed[key], config[key])
                    for argument, key in (("n_splits", "cv_splits"), ("sequence_length", "recurrent_sequence_length"),
                                          ("max_epochs", "recurrent_epochs"), ("batch_size", "recurrent_batch_size")):
                        self.assertEqual(passed[argument], config[key])


if __name__ == "__main__":
    unittest.main()
