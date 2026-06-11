import json
import os
import tempfile
import unittest

import joblib
import numpy as np
import pandas as pd

from src.time_series_training import (
    build_training_pipeline,
    calculate_regression_metrics,
    evaluate_baselines,
    split_time_series_data,
)


def make_feature_data(rows=100):
    """Tạo dữ liệu nhỏ, đủ cột để test pipeline mà không train model thật."""
    date_time = pd.date_range(
        "2024-01-01",
        periods=rows,
        freq="h",
    )
    traffic = np.arange(rows, dtype=float) + 100
    return pd.DataFrame(
        {
            "date_time": date_time,
            "is_holiday": [None] * rows,
            "weather_type": ["Clear"] * rows,
            "weather_description": ["clear sky"] * rows,
            "temperature": np.linspace(270, 290, rows),
            "humidity": np.linspace(40, 80, rows),
            "traffic_volume_lag_1h": traffic - 1,
            "traffic_volume_lag_24h": traffic - 24,
            "traffic_volume_lag_168h": traffic - 168,
            "traffic_volume": traffic,
        }
    )


class TimeSeriesTrainingTests(unittest.TestCase):
    def test_split_is_chronological_without_overlap(self):
        dataframe = make_feature_data()

        train_df, validation_df, test_df = split_time_series_data(
            dataframe
        )

        self.assertEqual(len(train_df), 70)
        self.assertEqual(len(validation_df), 15)
        self.assertEqual(len(test_df), 15)
        self.assertLess(
            train_df["date_time"].max(),
            validation_df["date_time"].min(),
        )
        self.assertLess(
            validation_df["date_time"].max(),
            test_df["date_time"].min(),
        )

    def test_split_sorts_unsorted_input(self):
        dataframe = make_feature_data().sample(
            frac=1,
            random_state=42,
        )

        train_df, _, _ = split_time_series_data(dataframe)

        self.assertTrue(train_df["date_time"].is_monotonic_increasing)

    def test_invalid_split_ratio_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "nhỏ hơn 1"):
            split_time_series_data(
                make_feature_data(),
                train_ratio=0.9,
                validation_ratio=0.2,
            )

    def test_metrics_are_calculated_correctly(self):
        metrics = calculate_regression_metrics(
            [100, 200],
            [90, 220],
        )

        self.assertEqual(metrics["MAE"], 15)
        self.assertAlmostEqual(metrics["RMSE"], 15.8114)
        self.assertEqual(metrics["WAPE"], 10)

    def test_naive_baselines_use_the_expected_lag_columns(self):
        dataframe = make_feature_data()

        baselines = evaluate_baselines(dataframe)

        self.assertEqual(baselines["NaiveLag1Hour"]["MAE"], 1)
        self.assertEqual(baselines["NaiveLag24Hours"]["MAE"], 24)

    def test_pipeline_handles_unseen_category(self):
        train_df = make_feature_data(30)
        validation_df = make_feature_data(5)
        validation_df["weather_type"] = "Snow"
        feature_columns = [
            column
            for column in train_df.columns
            if column not in ["date_time", "traffic_volume"]
        ]
        pipeline = build_training_pipeline(
            "LightGBM",
            feature_columns,
        )

        pipeline.fit(
            train_df[feature_columns],
            train_df["traffic_volume"],
        )
        predictions = pipeline.predict(validation_df[feature_columns])

        self.assertEqual(len(predictions), len(validation_df))

    def test_pipeline_artifact_can_be_saved_and_loaded(self):
        dataframe = make_feature_data(30)
        feature_columns = [
            column
            for column in dataframe.columns
            if column not in ["date_time", "traffic_volume"]
        ]
        pipeline = build_training_pipeline(
            "LightGBM",
            feature_columns,
        )
        pipeline.fit(
            dataframe[feature_columns],
            dataframe["traffic_volume"],
        )

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "model.pkl")
            joblib.dump(pipeline, path)
            loaded = joblib.load(path)

            predictions = loaded.predict(dataframe[feature_columns])
            self.assertEqual(len(predictions), len(dataframe))

    def test_report_structure_can_be_serialized(self):
        report = {
            "selected_model": "LightGBM",
            "test_metrics": calculate_regression_metrics(
                [100, 200],
                [90, 220],
            ),
        }

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump(report, file)
            with open(path, "r", encoding="utf-8") as file:
                saved = json.load(file)

        self.assertEqual(saved["selected_model"], "LightGBM")


if __name__ == "__main__":
    unittest.main()
