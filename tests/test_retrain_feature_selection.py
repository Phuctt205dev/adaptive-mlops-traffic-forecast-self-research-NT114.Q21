"""Synthetic fixtures only: time boundaries, per-model selection and saved inference."""
import copy
import json
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

from backend.app.services.retrain_selection import quality_decision, prepare_retrain_selection
from src import feature_selection as fs, pipeline, time_series_cross_validation as cv
from src.lstm_sequences import (SEQUENCE_NUMERIC_COLUMNS, fit_sequence_preprocessors, transform_sequence_source)


class SelectionTests(unittest.TestCase):
    def report(self, mae=100., paired=168, requested=168):
        return {"metrics": {"mae": mae}, "coverage": {"paired_points": paired, "requested_points": requested},
                "active_model_version_id": "same-model", "dataset_id": "same-dataset", "end_at": "2026-01-08"}

    def test_quality_requires_same_model_sufficient_coverage_and_15_percent(self):
        self.assertEqual(quality_decision(self.report(114), self.report())["status"], "acceptable")
        self.assertEqual(quality_decision(self.report(115), self.report())["status"], "acceptable")
        self.assertEqual(quality_decision(self.report(116), self.report())["status"], "degraded")
        for report in (self.report(paired=23), self.report(paired=130), self.report(mae=float("nan"))):
            self.assertEqual(quality_decision(report, self.report())["status"], "unknown")
        other = {**self.report(), "active_model_version_id": "other-model"}
        self.assertEqual(quality_decision(other, self.report())["status"], "unknown")
        self.assertEqual(quality_decision(self.report(1), self.report(0))["status"], "degraded")

    def test_quality_holdout_guard_discards_a_leaking_policy(self):
        config = {"enabled": True, "mode": "reuse", "profiles": {},
                  "decision": {"quality_window_end": "2026-01-08T00:00:00+07:00"}}
        guarded = fs.guard_quality_holdout(config, "2026-01-08")
        self.assertEqual(guarded["mode"], "refresh")
        self.assertEqual(guarded["decision"]["reason"], "quality_overlaps_holdout")

    def profile(self, **overrides):
        return {"status": "validated", "model_name": "RandomForest", "selected_groups": ["temperature"],
                "available_groups": ["temperature", "humidity"], "selection_train_end": "2025-01-03", **overrides}

    def test_reuse_is_model_schema_and_time_specific(self):
        config = {"mode": "reuse", "profiles": {"random_forest_lag": self.profile()}}
        names = ["temperature", "humidity"]
        self.assertEqual(fs.reusable_groups(config, "RandomForest", names, "2025-01-04"), ["temperature"])
        self.assertIsNone(fs.reusable_groups(config, "GRU", names, "2025-01-04"))
        self.assertIsNone(fs.reusable_groups(config, "RandomForest", names, "2025-01-02"))
        self.assertIsNone(fs.reusable_groups(config, "RandomForest", names + ["rain_p_h"], "2025-01-04"))
        config["profiles"]["random_forest_lag"]["selection_train_end"] = "NaT"
        self.assertIsNone(fs.reusable_groups(config, "RandomForest", names, "2025-01-04"))

    def test_bad_or_incomplete_reduction_preserves_full_inputs(self):
        folds = [{"full_metrics": [10, 11, 12], "reduced_metrics": [11, 12, 13],
                  "selected_groups": ["temperature"]}]
        result = fs.selection_summary("RandomForest", {}, folds, ["temperature", "humidity"])
        self.assertEqual(result["mode"], "full")
        folds[0]["reduced_metrics"] = [10.1, 11, 12]
        self.assertEqual(fs.selection_summary("RandomForest", {}, folds, ["temperature", "humidity"])["mode"], "reduced")
        folds.append({"full_metrics": [10, 11, 12], "reduced_metrics": None})
        self.assertEqual(fs.selection_summary("RandomForest", {}, folds, ["temperature", "humidity"])["mode"], "full")

    def test_real_shap_tiny_trees_and_calendar_grouping(self):
        X = pd.DataFrame({"temperature": np.arange(24, dtype=float), "humidity": np.zeros(24),
                          "hour_sin": np.zeros(24), "hour_cos": np.zeros(24),
                          "weather_type_Rain": [True, False] * 12})
        self.assertEqual(np.asarray(X).dtype, object)
        trainers = [RandomForestRegressor(n_estimators=3, max_depth=2, random_state=42)]
        from xgboost import XGBRegressor
        from lightgbm import LGBMRegressor
        trainers += [XGBRegressor(n_estimators=3, max_depth=2, n_jobs=1),
                     LGBMRegressor(n_estimators=3, min_child_samples=2, verbosity=-1, n_jobs=1)]
        for model in trainers:
            with self.subTest(model=type(model).__name__):
                model.fit(X, X.temperature)
                groups = fs.rank_training_features(model, X, list(X.columns), shap_config={"max_samples": 3})
                self.assertIn("temperature", groups)
                self.assertEqual(fs.feature_group("hour_sin"), fs.feature_group("hour_cos"))

    def test_actual_weather_preprocessing_runs_selection_and_final_report(self):
        raw = pd.DataFrame({"date_time": pd.date_range("2025-01-01", periods=96, freq="h"),
                            "is_holiday": [None] * 96, "temperature": np.arange(96.),
                            "weather_type": ["Clear", "Rain"] * 48,
                            "weather_description": ["clear", "rain"] * 48,
                            "traffic_volume": 100 + np.arange(96.)})
        captured = []
        def ranking(model, X, names, **kwargs):
            captured.append(X.dtypes.to_dict())
            return fs.rank_training_features(model, X, names, **kwargs)
        def tiny(X, y, random_state):
            return RandomForestRegressor(n_estimators=3, max_depth=3, random_state=random_state).fit(X, y)
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, "mlflow", pipeline._NullMLflow()), \
                patch.object(pipeline, "load_observed_target_data", return_value=raw), \
                patch.object(pipeline, "train_random_forest", side_effect=tiny), \
                patch.object(pipeline, "rank_training_features", side_effect=ranking):
            result = pipeline.run_pipeline("2025-01-01", "2025-02-01", artifact_root=tmp,
                selected_models=["random_forest_lag"], cv_splits=2,
                feature_selection={"enabled": True}, shap_config={"max_samples": 3})
        self.assertEqual(len(captured), 2)
        self.assertTrue(all(any(dtype == bool for dtype in dtypes.values()) for dtypes in captured))
        self.assertEqual(result["shap_report"]["status"], "completed")
        self.assertTrue(all(row.get("reason") != "TypeError"
                            for row in result["feature_profiles"]["random_forest_lag"]["folds"]))

    def test_saved_tree_wrapper_accepts_full_schema_and_ignores_dropped_inputs(self):
        X = pd.DataFrame({"temperature": np.arange(12.), "humidity": np.arange(12.)})
        model = RandomForestRegressor(n_estimators=2, random_state=1).fit(X[["temperature"]], X.temperature)
        wrapped = fs.wrap_tree_model(model, list(X.columns), ["temperature"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.pkl"
            joblib.dump(wrapped, path)
            restored = joblib.load(path)
            np.testing.assert_array_equal(restored.predict(X), restored.predict(X.assign(humidity=99999)))
            self.assertEqual(list(restored.feature_names_in_), list(X.columns))

    def test_sequence_mask_roundtrips_and_legacy_preprocessors_unchanged(self):
        source = pd.DataFrame({key: np.arange(10, dtype=float) for key in SEQUENCE_NUMERIC_COLUMNS})
        source["weather_type"], source["traffic_volume"] = "Clear", np.arange(10.)
        source["date_time"] = pd.date_range("2025-01-01", periods=10, freq="h")
        pre = fit_sequence_preprocessors(source, np.arange(3, 8))
        full = transform_sequence_source(source, pre)
        original = copy.deepcopy(pre)
        indices = [0, 3]
        pre["selected_feature_indices"] = indices
        pre["all_sequence_feature_names"] = pre["sequence_feature_names"]
        pre["sequence_feature_names"] = [pre["sequence_feature_names"][i] for i in indices]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pre.joblib"
            joblib.dump(pre, path)
            np.testing.assert_array_equal(transform_sequence_source(source, joblib.load(path)), full[:, indices])
        np.testing.assert_array_equal(transform_sequence_source(source, original), full)
        pre["selected_feature_indices"] = [0, 0]
        with self.assertRaises(ValueError):
            transform_sequence_source(source, pre)

    def tiny_pipeline(self, *, selecting=True, fail_rank=False):
        raw = pd.DataFrame({"date_time": pd.date_range("2025-01-01", periods=90, freq="h"),
                            "temperature": np.arange(90.), "humidity": np.zeros(90), "traffic_volume": np.arange(90.)})
        ranking_bounds, inputs = [], []
        def trainer(X, y, seed):
            inputs.append((list(X.columns), X.index.max()))
            return RandomForestRegressor(n_estimators=2, random_state=seed).fit(X, y)
        def rank(model, X, names, **kwargs):
            ranking_bounds.append(X.index.max())
            if fail_rank:
                raise TimeoutError("synthetic budget")
            return ["temperature"]
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, "mlflow", pipeline._NullMLflow()), \
                patch.object(pipeline, "preprocess", side_effect=lambda frame: frame), \
                patch.object(pipeline, "load_observed_target_data", return_value=raw), \
                patch.object(pipeline, "train_random_forest", side_effect=trainer), \
                patch.object(pipeline, "rank_training_features", side_effect=rank):
            info = pipeline.run_pipeline("2025-01-01", "2025-02-01", artifact_root=tmp,
                       selected_models=["random_forest_lag"], cv_splits=2,
                       feature_selection={"enabled": True} if selecting else None,
                       shap_config={"max_samples": 2})
            restored = joblib.load(info["versioned_model_file"])
            self.assertEqual(info["shap_report"]["status"], "completed")
            if selecting:
                profile = info["feature_profiles"]["random_forest_lag"]
                self.assertEqual(profile["mode"], "full" if fail_rank else "reduced")
                self.assertFalse(profile["final_test_used"])
                self.assertTrue(all(bound < 76 for bound in ranking_bounds))
                for row in profile["folds"]:
                    self.assertLess(pd.Timestamp(row["train_end"]), pd.Timestamp(row["validation_start"]))
            else:
                self.assertNotIn("feature_profiles", info)
                self.assertFalse(ranking_bounds)
            self.assertEqual(len(restored.predict(raw.drop(columns=["date_time", "traffic_volume"]))), len(raw))
            return inputs

    def test_tree_end_to_end_selects_within_folds_and_persists_inference(self):
        self.tiny_pipeline()

    def test_first_training_keeps_full_inputs_and_shap(self):
        calls = self.tiny_pipeline(selecting=False)
        self.assertTrue(all(columns == ["temperature", "humidity"] for columns, _ in calls))

    def test_shap_timeout_falls_back_to_full_training(self):
        self.tiny_pipeline(fail_rank=True)

    def test_neural_selection_uses_training_tensor_and_checks_both_models(self):
        for name in ("LSTM", "GRU"):
            shapes = []
            validations = []
            class FakeModel:
                def fit(self, X, y, **kwargs):
                    shapes.append(X.shape)
                    validations.append(kwargs["validation_data"][0])
                    return SimpleNamespace(history={"val_loss": [1.]}, epoch=[0])
                def predict(self, X, **kwargs):
                    return np.zeros((len(X), 1))
            keras = SimpleNamespace(backend=SimpleNamespace(clear_session=lambda: None))
            modules = {"tensorflow": SimpleNamespace(keras=keras),
                       "src.models.recurrent": SimpleNamespace(build_recurrent_model=lambda *a, **kw: FakeModel(),
                                                                build_training_callbacks=lambda: []),
                       "src.neural_utils": SimpleNamespace(configure_reproducibility=lambda *a: None,
                                                           inverse_scale_predictions=lambda p, s: p.reshape(-1))}
            datasets = {key: {"X": np.ones((count, 3, 2)), "y": np.ones((count, 1)),
                              "raw_y": np.ones(count)} for key, count in (("train", 6), ("validation", 2))}
            row = {}
            original_train = datasets["train"]["X"]
            original_validation = datasets["validation"]["X"]
            with self.subTest(model=name), patch.dict(sys.modules, modules), \
                    patch.object(cv, "epoch_callbacks", return_value=[]), \
                    patch.object(cv, "rank_training_features", return_value=["temperature"]) as ranking:
                cv._train_neural_fold(name, datasets, None, 1, 2, 42, 0,
                    selection_config={"enabled": True}, selection_row=row,
                    feature_names=["temperature", "humidity"])
                self.assertIs(ranking.call_args.args[1], original_train)
                self.assertEqual(shapes, [(5, 3, 2), (5, 3, 1)])
                self.assertTrue(all(not np.shares_memory(value, original_validation) for value in validations))
                self.assertEqual(datasets["train"]["X"].shape, (6, 3, 1))
                self.assertEqual(row["status"], "compared")
                self.assertEqual(row["reduced_metrics"][0], 1.)
                self.assertEqual(set(row["reduced_validation_metrics"]), {"MAE", "RMSE", "MAPE", "WAPE", "R2"})

    def test_unknown_quality_without_profiles_does_not_load_models(self):
        with patch("backend.app.services.retrain_selection.model_quality.get_model_quality") as quality:
            selection = prepare_retrain_selection(None, None, None, SimpleNamespace(configuration_json={}), None,
                                                   {"shap": {}}, pd.DataFrame())
        self.assertEqual(selection["mode"], "refresh")
        quality.assert_not_called()

    def test_selection_policy_reuses_only_after_matched_quality_windows(self):
        frame = pd.DataFrame({"date_time": pd.date_range("2025-01-01", periods=24 * 60, freq="h")})
        training = SimpleNamespace(configuration_json={"train_end_date": "2025-01-03",
                   "feature_profiles": {"random_forest_lag": self.profile()}})
        config = {"train_start_date": "2025-01-01", "train_end_date": "2025-03-01", "shap": {}}
        with patch("backend.app.services.retrain_selection.preprocess", side_effect=lambda f: f), \
                patch("backend.app.services.retrain_selection.model_quality.get_model_quality",
                      side_effect=[self.report(110), self.report()]) as quality:
            selection = prepare_retrain_selection(None, SimpleNamespace(id="region"), SimpleNamespace(id="model"),
                                                   training, None, config, frame)
        self.assertEqual(selection["mode"], "reuse")
        self.assertEqual(quality.call_count, 2)
        self.assertLess(pd.Timestamp(quality.call_args_list[0].kwargs["start_at"]), pd.Timestamp("2025-03-01"))

    def test_neural_dataset_preprocessing_persists_reduced_channels(self):
        source = pd.DataFrame({key: np.arange(14, dtype=float) for key in SEQUENCE_NUMERIC_COLUMNS})
        source["weather_type"], source["traffic_volume"] = "Clear", np.arange(14.)
        source["date_time"] = pd.date_range("2025-01-01", periods=14, freq="h")
        datasets, pre = cv._build_neural_fold_datasets(source, source.date_time.iloc[3:9],
                          source.date_time.iloc[9:12], 3, selected_groups=["temperature"])
        self.assertEqual(datasets["train"]["X"].shape[-1], 1)
        self.assertEqual(pre["sequence_feature_names"], ["temperature"])
        self.assertEqual(transform_sequence_source(source, pre).shape, (14, 1))


if __name__ == "__main__":
    unittest.main()
