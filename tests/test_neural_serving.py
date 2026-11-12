"""Serving regression without real training, network, or production data."""
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from backend.app.core.errors import ApplicationError
from backend.app.services import predictions
from src import time_series_preprocess as prep
from tests.test_prediction_service import build_prediction_csv, fake_s3_client
from tests.test_time_series_preprocess import make_row


class HourlyServingPreparationTests(unittest.TestCase):
    def test_unique_hours_match_original_grouped_mean_and_category_mode(self):
        for kind in ("normal", "missing", "nullable", "missing_time"):
            with self.subTest(kind=kind):
                frame = pd.DataFrame([make_row(when, 100 + index) for index, when in enumerate(
                    pd.date_range("2026-01-01", periods=48, freq="h"))])
                frame.loc[0, "is_holiday"] = "Holiday"
                if kind in {"missing", "nullable"}:
                    frame.loc[1, ["temperature", "traffic_volume", "weather_type"]] = np.nan
                if kind == "nullable":
                    frame["humidity"] = frame["humidity"].astype("Int64")
                    frame["weather_type"] = frame["weather_type"].astype("string")
                if kind == "missing_time":
                    frame.loc[1, "date_time"] = pd.NaT
                frame = frame.sample(frac=1, random_state=42)
                aggregation = {name: "mean" for name in prep.NUMERIC_FEATURE_COLUMNS}
                aggregation.update({name: prep.most_common_value for name in ("is_holiday", *prep.CATEGORICAL_COLUMNS)})
                aggregation["traffic_volume"] = "mean"
                expected = frame.groupby("date_time").agg(aggregation).sort_index()
                expected = expected.join(frame.groupby("date_time").size().rename("source_row_count"))
                expected["is_observed_hour"] = True
                expected["target_observed"] = expected.traffic_volume.notna()
                pd.testing.assert_frame_equal(prep.aggregate_duplicate_hours(frame), expected)

    def test_serving_skips_offline_report_without_changing_rows_or_audit(self):
        frame = pd.DataFrame([make_row(when, 100 + index) for index, when in enumerate(
            pd.date_range("2026-01-01", periods=72, freq="h"))])
        # Include duplicates and a missing hour, not just the unique fast path.
        frame = pd.concat([frame.drop(index=[30]), frame.iloc[[2]]], ignore_index=True)
        expected_hourly, expected_audit, report = prep.prepare_hourly_time_series(frame)
        self.assertIn("target_imputation_backtest", report)
        with patch.object(prep, "build_quality_report", side_effect=AssertionError("Offline report must not run")):
            hourly, audit, skipped_report = prep.prepare_hourly_time_series(frame, include_quality_report=False)
        pd.testing.assert_frame_equal(hourly, expected_hourly)
        pd.testing.assert_frame_equal(audit, expected_audit)
        self.assertEqual(skipped_report, {})

    def test_serving_reuses_prepared_data_and_invalidates_when_dataset_changes(self):
        for function in (predictions._hourly_context_cached, predictions._download_dataset_frame_cached):
            function.cache_clear()
            self.addCleanup(function.cache_clear)
        with patch.object(predictions, "get_s3_client", return_value=fake_s3_client(build_prediction_csv())) as storage, \
             patch.object(predictions, "prepare_hourly_time_series", wraps=prep.prepare_hourly_time_series) as prepare:
            first = predictions._hourly_context_cached("s3://synthetic/serving.csv", "a" * 64)
            self.assertIs(predictions._hourly_context_cached("s3://synthetic/serving.csv", "a" * 64), first)
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(storage.call_count, 1)
            prepare.assert_called_with(prepare.call_args.args[0], include_quality_report=False)
            predictions._hourly_context_cached("s3://synthetic/serving.csv", "b" * 64)
            self.assertEqual(prepare.call_count, 2)
            self.assertEqual(storage.call_count, 2)


class NeuralServingTests(unittest.TestCase):
    def test_loader_skips_compile_and_reuses_the_loaded_model(self):
        predictions._load_keras_model_from_path.cache_clear()
        self.addCleanup(predictions._load_keras_model_from_path.cache_clear)
        loaded = object()
        load = Mock(return_value=loaded)
        tensorflow = SimpleNamespace(keras=SimpleNamespace(models=SimpleNamespace(load_model=load)))
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"tensorflow": tensorflow}):
            path = Path(directory) / "synthetic.keras"
            path.write_bytes(b"synthetic fixture; loader is mocked")
            self.assertIs(predictions._load_keras_model_from_path(str(path)), loaded)
            self.assertIs(predictions._load_keras_model_from_path(str(path)), loaded)
            load.assert_called_once_with(str(path), compile=False)

    def test_forward_is_bounded_and_always_uses_inference_mode(self):
        model = Mock(side_effect=lambda values, training: values[:, -1, :1])
        values = np.arange(600 * 3 * 2).reshape(600, 3, 2)
        result = predictions._neural_forward(model, values)
        np.testing.assert_array_equal(result, values[:, -1, :1])
        self.assertEqual([len(call.args[0]) for call in model.call_args_list], [256, 256, 88])
        self.assertTrue(all(call.kwargs == {"training": False} for call in model.call_args_list))
        self.assertTrue(all(call.args[0].dtype == np.float32 for call in model.call_args_list))

    def test_single_and_dashboard_use_identical_causal_sequences_and_scaling(self):
        times = pd.date_range("2026-06-01", periods=12, freq="h").to_numpy()
        transformed = np.arange(24, dtype=np.float32).reshape(12, 2)
        preprocessors = {"target_scaler": SimpleNamespace(inverse_transform=lambda values: values * 10 + 100),
                         "sequence_feature_names": ["temperature", "traffic_history_value"]}
        model = Mock(side_effect=lambda values, training: values[:, -1, :1])
        version = SimpleNamespace(artifact_uri="models/synthetic.keras")
        run = SimpleNamespace(configuration_json={"preprocessor_file": "models/pre.pkl", "recurrent_sequence_length": 3})
        dataset = SimpleNamespace(id="synthetic", storage_uri="s3://synthetic/serving.csv", sha256="a" * 64)
        with patch.object(predictions, "_load_keras_model", return_value=model), \
             patch.object(predictions, "_neural_sequence_context", return_value=(times, transformed, preprocessors)):
            single, metadata = predictions._predict_neural_sequence(version, run, dataset, pd.Timestamp("2026-06-01T08:00:00"))
            batch = predictions._predict_neural_sequence_batch(version, run, dataset, [
                {"date_time": "2026-06-01T08:00:00"}, {"date_time": "2026-06-01T09:00:00"}])
            self.assertEqual(single, 240)
            self.assertEqual(batch, {datetime(2026, 6, 1, 8): 240, datetime(2026, 6, 1, 9): 260})
            self.assertEqual(metadata["sequence_length"], 3)
            np.testing.assert_array_equal(model.call_args_list[0].args[0], transformed[5:8][None, :, :])
            np.testing.assert_array_equal(model.call_args_list[1].args[0][0], transformed[5:8])
            model.predict.assert_not_called()

    def test_insufficient_history_still_rejects_before_model_forward(self):
        model = Mock()
        run = SimpleNamespace(configuration_json={"preprocessor_file": "models/pre.pkl", "recurrent_sequence_length": 72})
        dataset = SimpleNamespace(storage_uri="s3://synthetic/serving.csv", sha256="a" * 64)
        context = (pd.date_range("2026-01-01", periods=10, freq="h").to_numpy(), np.zeros((10, 2)), {})
        with patch.object(predictions, "_load_keras_model", return_value=model), \
             patch.object(predictions, "_neural_sequence_context", return_value=context):
            with self.assertRaises(ApplicationError) as error:
                predictions._predict_neural_sequence(SimpleNamespace(), run, dataset, pd.Timestamp("2026-01-01T03:00:00"))
            self.assertEqual(error.exception.code, "prediction_history_insufficient")
            model.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("tensorflow"), "TensorFlow is exercised by the full Linux CI suite")
    def test_real_lstm_and_gru_saved_models_match_predict_without_training(self):
        from tensorflow import keras
        from src.models.recurrent import build_recurrent_model
        random = np.random.default_rng(42)
        values = random.normal(size=(5, 4, 3)).astype(np.float32)
        for name in ("LSTM", "GRU"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                model = build_recurrent_model(name, (4, 3), recurrent_units=4, dense_units=2)
                path = str(Path(directory) / (name + ".keras"))
                model.save(path)
                loaded = predictions._load_keras_model_from_path(path)
                np.testing.assert_allclose(predictions._neural_forward(loaded, values),
                                           model.predict(values, verbose=0), rtol=1e-5, atol=1e-5)
                self.assertIsNone(getattr(loaded, "optimizer", None))
                predictions._load_keras_model_from_path.cache_clear()
                keras.backend.clear_session()


if __name__ == "__main__":
    unittest.main()
