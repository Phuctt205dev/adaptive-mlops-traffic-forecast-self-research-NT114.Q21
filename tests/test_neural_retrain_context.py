from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src import time_series_cross_validation as cv
from src.feature_selection import feature_group
from src.region_training_variants import _prepare_region_time_series_inputs
from tests.test_region_pipeline import make_raw_training_csv


class NeuralRetrainContextTests(unittest.TestCase):
    def test_reduced_cv_preserves_all_metrics_through_final_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.csv"
            make_raw_training_csv(path, rows=24 * 30)
            prepared = _prepare_region_time_series_inputs(path, tmp, "synthetic", "probe", "lstm")
            full = {"MAE": 10., "RMSE": 12., "MAPE": 15., "WAPE": 30., "R2": -.4}
            reduced = {"MAE": 9., "RMSE": 10., "MAPE": 12., "WAPE": 7., "R2": .4}
            def train_fold(*args, **kwargs):
                if "selection_row" not in kwargs:
                    return dict(reduced), 1, 1, .01
                row = kwargs["selection_row"]
                row.update(full_metrics=[10., 12., 15.], reduced_metrics=[9., 10., 12.],
                           reduced_validation_metrics=dict(reduced), selected_groups=["temperature"],
                           reduced_best_epoch=1, reduced_epochs=1, status="compared")
                return dict(full), 1, 1, .01
            final_result = {"metrics": reduced, "training_seconds": .01, "model_size_bytes": 1,
                            "preprocessor_path": "synthetic.pkl", "preprocessor_size_bytes": 1,
                            "shap_report": {"status": "completed"}, "selected_feature_names": ["temperature"]}
            for name in ("LSTM", "GRU"):
                with self.subTest(model=name), patch.object(cv, "_train_neural_fold", side_effect=train_fold), \
                        patch.object(cv, "_train_final_neural_model", return_value=final_result) as final:
                    result = cv._run_neural_cross_validation(name, prepared["feature_df"], prepared["hourly_df"],
                        prepared["audit_df"], 2, 72, 1, 8, 42, str(Path(tmp) / "m.keras"),
                        str(Path(tmp) / "pre.pkl"), str(Path(tmp) / "pred.csv"), 0,
                        train_start_date="2025-01-12", train_end_date="2025-01-25", feature_selection={"enabled": True})
                    self.assertEqual(result["feature_selection_result"]["mode"], "reduced")
                    self.assertEqual(set(result["cv_metrics"]), set(reduced))
                    self.assertEqual(result["cv_metrics"]["WAPE"]["mean"], 7.)
                    self.assertEqual(result["cv_metrics"]["R2"]["mean"], .4)
                    self.assertEqual(final.call_args.kwargs["selected_groups"], ["temperature"])

                    source = cv._prepare_neural_sequence_source(prepared["hourly_df"], prepared["audit_df"],
                        "2025-01-12", "2025-01-25", 72)
                    _, pre = cv._build_neural_fold_datasets(source, pd.date_range("2025-01-12", periods=24, freq="h"),
                        pd.date_range("2025-01-13", periods=12, freq="h"), 72)
                    profile = {"status": "validated", "model_name": name, "sequence_length": 72,
                        "available_groups": sorted({feature_group(value) for value in pre["sequence_feature_names"]}),
                        "selected_groups": ["temperature"], "selection_train_end": "2025-01-11"}
                    reused = cv._run_neural_cross_validation(name, prepared["feature_df"], prepared["hourly_df"],
                        prepared["audit_df"], 2, 72, 1, 8, 42, str(Path(tmp) / "m.keras"),
                        str(Path(tmp) / "pre.pkl"), str(Path(tmp) / "pred.csv"), 0,
                        train_start_date="2025-01-12", train_end_date="2025-01-25",
                        feature_selection={"enabled": True, "mode": "reuse", "profiles": {name.lower(): profile}})
                    self.assertTrue(reused["feature_selection_result"]["profile_reused"])
                    self.assertEqual(reused["cv_metrics"]["WAPE"]["mean"], 7.)
                    self.assertEqual(reused["cv_metrics"]["R2"]["mean"], .4)

    def test_mid_dataset_window_keeps_history_but_not_extra_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.csv"
            make_raw_training_csv(path, rows=24 * 30)
            prepared = _prepare_region_time_series_inputs(path, tmp, "synthetic", "probe", "lstm")
            start, end = pd.Timestamp("2025-01-12"), pd.Timestamp("2025-01-25")
            source = cv._prepare_neural_sequence_source(prepared["hourly_df"], prepared["audit_df"], start, end, 72)
            self.assertEqual(source.date_time.min(), start - pd.Timedelta(hours=72 + 168))
            self.assertEqual(source.date_time.max(), end)
            train = pd.date_range(start, periods=96, freq="h")
            validation = pd.date_range(train[-1] + pd.Timedelta(hours=1), periods=24, freq="h")
            datasets, preprocessors = cv._build_neural_fold_datasets(source, train, validation, 72)
            np.testing.assert_array_equal(datasets["train"]["timestamps"], train.to_numpy())
            self.assertEqual(preprocessors["last_train_history_time"], train[-1] - pd.Timedelta(hours=1))
            # Encoder/scaler exclude the extra 168h causal warmup and every
            # validation/final-test row. Exactly 72h context plus train history.
            self.assertEqual(preprocessors["feature_scaler"].n_samples_seen_, 72 + len(train) - 1)

    def test_real_cv_reaches_fit_for_lstm_and_gru_after_a_rolling_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.csv"
            make_raw_training_csv(path, rows=24 * 30)
            prepared = _prepare_region_time_series_inputs(path, tmp, "synthetic", "probe", "lstm")
            for name in ("LSTM", "GRU"):
                with self.subTest(model=name), patch.object(cv, "_train_neural_fold", side_effect=RuntimeError("reached_fit")):
                    with self.assertRaisesRegex(RuntimeError, "reached_fit"):
                        cv._run_neural_cross_validation(name, prepared["feature_df"], prepared["hourly_df"],
                            prepared["audit_df"], 2, 72, 1, 8, 42, str(Path(tmp) / "m.keras"),
                            str(Path(tmp) / "pre.pkl"), str(Path(tmp) / "pred.csv"), 0,
                            train_start_date="2025-01-12", train_end_date="2025-01-25", feature_selection={"enabled": True})

    def test_dataset_start_has_no_invented_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.csv"
            make_raw_training_csv(path, rows=24 * 30)
            prepared = _prepare_region_time_series_inputs(path, tmp, "synthetic", "probe", "lstm")
            source = cv._prepare_neural_sequence_source(prepared["hourly_df"], prepared["audit_df"],
                         "2025-01-01", "2025-01-25", 72)
            self.assertEqual(source.date_time.min(), pd.Timestamp("2025-01-01"))
            self.assertEqual(cv._sequence_preprocessor_start(source), 0)
            with self.assertRaisesRegex(ValueError, "sequence"):
                cv._build_neural_fold_datasets(source, source.date_time.iloc[:10], source.date_time.iloc[10:12], 72)


if __name__ == "__main__":
    unittest.main()
