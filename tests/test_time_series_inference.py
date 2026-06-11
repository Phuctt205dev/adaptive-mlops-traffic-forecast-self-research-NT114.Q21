import os
import tempfile
import unittest

import numpy as np
import pandas as pd

from src.time_series_features import create_time_series_features
from src.time_series_inference import (
    EXOGENOUS_COLUMNS,
    InsufficientHistoryError,
    TrafficHistory,
    build_next_hour_feature_row,
    predict_next_hour,
    run_sequential_backtest,
)
from src.time_series_preprocess import ORIGINAL_COLUMN_ORDER


def make_hourly_and_audit(periods=220):
    date_time = pd.date_range(
        "2024-01-01",
        periods=periods,
        freq="h",
    )
    traffic = np.arange(periods, dtype=float) + 1000
    hourly = pd.DataFrame(
        {
            "date_time": date_time,
            "is_holiday": [None] * periods,
            "air_pollution_index": [100] * periods,
            "humidity": [60] * periods,
            "wind_speed": [5] * periods,
            "wind_direction": [180] * periods,
            "visibility_in_miles": [10] * periods,
            "dew_point": [10] * periods,
            "temperature": [280.0] * periods,
            "rain_p_h": [0.0] * periods,
            "snow_p_h": [0.0] * periods,
            "clouds_all": [20] * periods,
            "weather_type": ["Clear"] * periods,
            "weather_description": ["clear sky"] * periods,
            "traffic_volume": traffic,
        }
    )[ORIGINAL_COLUMN_ORDER]
    audit = pd.DataFrame(
        {
            "date_time": date_time,
            "target_observed": [True] * periods,
        }
    )
    return hourly, audit


def exogenous_from_row(row):
    return {
        column: row[column]
        for column in EXOGENOUS_COLUMNS
    }


class LastLagModel:
    """Model giả dự đoán bằng lag một giờ để test luồng inference."""

    def __init__(self, feature_names):
        self.feature_names_in_ = np.asarray(feature_names)

    def predict(self, dataframe):
        return dataframe["traffic_volume_lag_1h"].to_numpy()


class TimeSeriesInferenceTests(unittest.TestCase):
    def test_requires_168_hours_of_history(self):
        hourly, audit = make_hourly_and_audit(100)
        history_df = hourly.merge(audit, on="date_time")
        history = TrafficHistory.from_dataframe(history_df)

        with self.assertRaisesRegex(
            InsufficientHistoryError,
            "168 giờ",
        ):
            history.build_history_features(
                hourly.iloc[-1]["date_time"]
                + pd.Timedelta(hours=1)
            )

    def test_rejects_non_next_hour_prediction(self):
        hourly, audit = make_hourly_and_audit()
        history_df = hourly.iloc[:180].merge(
            audit.iloc[:180],
            on="date_time",
        )
        history = TrafficHistory.from_dataframe(history_df)

        with self.assertRaisesRegex(ValueError, "giờ kế tiếp"):
            history.build_history_features(
                hourly.iloc[181]["date_time"]
            )

    def test_lag_and_rolling_use_only_previous_hours(self):
        hourly, audit = make_hourly_and_audit()
        history_df = hourly.iloc[:180].merge(
            audit.iloc[:180],
            on="date_time",
        )
        history = TrafficHistory.from_dataframe(history_df)
        target_time = hourly.iloc[180]["date_time"]

        features = history.build_history_features(target_time)

        self.assertEqual(
            features["traffic_volume_lag_1h"],
            hourly.iloc[179]["traffic_volume"],
        )
        self.assertEqual(
            features["traffic_volume_lag_168h"],
            hourly.iloc[12]["traffic_volume"],
        )
        self.assertEqual(
            features["traffic_volume_rolling_3h_mean"],
            hourly.iloc[177:180]["traffic_volume"].mean(),
        )

    def test_missing_target_uses_past_week_without_future_data(self):
        hourly, audit = make_hourly_and_audit()
        history_df = hourly.iloc[:180].merge(
            audit.iloc[:180],
            on="date_time",
        )
        history_df.loc[179, "target_observed"] = False
        history_df.loc[179, "traffic_volume"] = 999999

        history = TrafficHistory.from_dataframe(history_df)
        features = history.build_history_features(
            hourly.iloc[180]["date_time"]
        )

        self.assertEqual(
            features["traffic_volume_lag_1h"],
            hourly.iloc[11]["traffic_volume"],
        )
        self.assertEqual(features["lag_1h_target_observed"], 0)

    def test_single_hour_features_match_batch_feature_builder(self):
        hourly, audit = make_hourly_and_audit()
        batch_features, _ = create_time_series_features(
            hourly,
            audit,
        )
        target_time = hourly.iloc[180]["date_time"]
        expected = batch_features[
            batch_features["date_time"] == target_time
        ].iloc[0]

        history_df = hourly.iloc[:180].merge(
            audit.iloc[:180],
            on="date_time",
        )
        history = TrafficHistory.from_dataframe(history_df)
        actual = build_next_hour_feature_row(
            target_time,
            exogenous_from_row(hourly.iloc[180]),
            history,
            expected_feature_columns=[
                column
                for column in batch_features.columns
                if column not in ["date_time", "traffic_volume"]
            ],
        ).iloc[0]

        for column in actual.index:
            if isinstance(actual[column], str) or pd.isna(
                actual[column]
            ):
                self.assertEqual(
                    str(actual[column]),
                    str(expected[column]),
                )
            else:
                self.assertAlmostEqual(
                    float(actual[column]),
                    float(expected[column]),
                )

    def test_prediction_uses_expected_schema(self):
        hourly, audit = make_hourly_and_audit()
        batch_features, _ = create_time_series_features(
            hourly,
            audit,
        )
        feature_columns = [
            column
            for column in batch_features.columns
            if column not in ["date_time", "traffic_volume"]
        ]
        model = LastLagModel(feature_columns)
        history_df = hourly.iloc[:180].merge(
            audit.iloc[:180],
            on="date_time",
        )
        history = TrafficHistory.from_dataframe(history_df)

        prediction, feature_row = predict_next_hour(
            model,
            hourly.iloc[180]["date_time"],
            exogenous_from_row(hourly.iloc[180]),
            history,
        )

        self.assertEqual(
            list(feature_row.columns),
            feature_columns,
        )
        self.assertEqual(
            prediction,
            hourly.iloc[179]["traffic_volume"],
        )

    def test_history_rejects_gap(self):
        history = TrafficHistory()
        history.append("2024-01-01 00:00:00", 100, True)

        with self.assertRaisesRegex(ValueError, "liên tục"):
            history.append("2024-01-01 02:00:00", 200, True)

    def test_sequential_backtest_predicts_before_updating_history(self):
        hourly, audit = make_hourly_and_audit()
        batch_features, _ = create_time_series_features(
            hourly,
            audit,
        )
        feature_columns = [
            column
            for column in batch_features.columns
            if column not in ["date_time", "traffic_volume"]
        ]
        model = LastLagModel(feature_columns)

        with tempfile.TemporaryDirectory() as directory:
            report, predictions = run_sequential_backtest(
                model=model,
                hourly_df=hourly,
                audit_df=audit,
                start_time=hourly.iloc[180]["date_time"],
                end_time=hourly.iloc[185]["date_time"],
                predictions_path=os.path.join(
                    directory,
                    "predictions.csv",
                ),
                report_path=os.path.join(
                    directory,
                    "report.json",
                ),
            )

        self.assertEqual(len(predictions), 6)
        self.assertEqual(
            predictions.iloc[0]["prediction"],
            hourly.iloc[179]["traffic_volume"],
        )
        self.assertTrue(report["prediction_before_history_update"])


if __name__ == "__main__":
    unittest.main()
