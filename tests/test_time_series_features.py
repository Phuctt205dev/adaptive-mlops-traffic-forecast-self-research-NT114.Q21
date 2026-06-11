import json
import os
import tempfile
import unittest

import pandas as pd

from src.time_series_features import (
    create_time_series_features,
    merge_hourly_with_audit,
    save_time_series_features,
)


def make_hourly_data(periods=10):
    """Tạo chuỗi nhỏ, traffic tăng đều để dễ tính kết quả bằng tay."""
    date_time = pd.date_range(
        "2024-01-01 00:00:00",
        periods=periods,
        freq="h",
    )
    hourly_df = pd.DataFrame(
        {
            "date_time": date_time,
            "is_holiday": [None] * periods,
            "air_pollution_index": [100] * periods,
            "humidity": [60] * periods,
            "wind_speed": [5] * periods,
            "wind_direction": [180] * periods,
            "visibility_in_miles": [10] * periods,
            "dew_point": [10] * periods,
            "temperature": [280] * periods,
            "rain_p_h": [0] * periods,
            "snow_p_h": [0] * periods,
            "clouds_all": [20] * periods,
            "weather_type": ["Clear"] * periods,
            "weather_description": ["clear sky"] * periods,
            "traffic_volume": list(range(1, periods + 1)),
        }
    )
    audit_df = pd.DataFrame(
        {
            "date_time": date_time,
            "target_observed": [True] * periods,
        }
    )
    return hourly_df, audit_df


class TimeSeriesFeatureTests(unittest.TestCase):
    def test_lag_uses_only_previous_rows(self):
        hourly_df, audit_df = make_hourly_data()

        feature_df, _ = create_time_series_features(
            hourly_df,
            audit_df,
            lag_hours=[1],
            rolling_windows=[3],
        )
        row = feature_df.iloc[0]

        self.assertEqual(row["traffic_volume"], 4)
        self.assertEqual(row["traffic_volume_lag_1h"], 3)

    def test_rolling_window_does_not_include_current_target(self):
        hourly_df, audit_df = make_hourly_data()

        feature_df, _ = create_time_series_features(
            hourly_df,
            audit_df,
            lag_hours=[1],
            rolling_windows=[3],
        )
        row = feature_df.iloc[0]

        self.assertEqual(row["traffic_volume"], 4)
        self.assertEqual(
            row["traffic_volume_rolling_3h_mean"],
            2,
        )
        self.assertEqual(
            row["traffic_volume_rolling_3h_max"],
            3,
        )

    def test_current_target_change_does_not_change_its_features(self):
        hourly_df, audit_df = make_hourly_data()
        changed_df = hourly_df.copy()
        changed_df.loc[3, "traffic_volume"] = 9999

        original, _ = create_time_series_features(
            hourly_df,
            audit_df,
            lag_hours=[1],
            rolling_windows=[3],
        )
        changed, _ = create_time_series_features(
            changed_df,
            audit_df,
            lag_hours=[1],
            rolling_windows=[3],
        )

        original_row = original.iloc[0].drop("traffic_volume")
        changed_row = changed.iloc[0].drop("traffic_volume")
        pd.testing.assert_series_equal(original_row, changed_row)

    def test_unobserved_target_is_not_used_as_training_label(self):
        hourly_df, audit_df = make_hourly_data()
        audit_df.loc[5, "target_observed"] = False

        feature_df, report = create_time_series_features(
            hourly_df,
            audit_df,
            lag_hours=[1],
            rolling_windows=[3],
        )

        removed_time = hourly_df.loc[5, "date_time"]
        self.assertNotIn(removed_time, set(feature_df["date_time"]))
        self.assertEqual(report["removed_unobserved_target_rows"], 1)

    def test_imputed_history_is_marked_by_lag_flag(self):
        hourly_df, audit_df = make_hourly_data()
        audit_df.loc[2, "target_observed"] = False

        feature_df, _ = create_time_series_features(
            hourly_df,
            audit_df,
            lag_hours=[1],
            rolling_windows=[3],
        )
        row = feature_df[
            feature_df["date_time"] == hourly_df.loc[3, "date_time"]
        ].iloc[0]

        self.assertEqual(row["lag_1h_target_observed"], 0)
        self.assertAlmostEqual(row["history_observed_ratio_3h"], 2 / 3)

    def test_unobserved_target_value_is_ignored_by_future_features(self):
        hourly_df, audit_df = make_hourly_data()
        audit_df.loc[2, "target_observed"] = False

        original, _ = create_time_series_features(
            hourly_df,
            audit_df,
            lag_hours=[1],
            rolling_windows=[3],
        )

        changed_df = hourly_df.copy()
        changed_df.loc[2, "traffic_volume"] = 9999
        changed, _ = create_time_series_features(
            changed_df,
            audit_df,
            lag_hours=[1],
            rolling_windows=[3],
        )

        comparison_time = hourly_df.loc[3, "date_time"]
        original_row = original[
            original["date_time"] == comparison_time
        ].iloc[0]
        changed_row = changed[
            changed["date_time"] == comparison_time
        ].iloc[0]

        self.assertEqual(
            original_row["traffic_volume_lag_1h"],
            changed_row["traffic_volume_lag_1h"],
        )
        self.assertEqual(
            original_row["traffic_volume_rolling_3h_mean"],
            changed_row["traffic_volume_rolling_3h_mean"],
        )

    def test_rejects_non_hourly_input(self):
        hourly_df, audit_df = make_hourly_data()
        hourly_df = hourly_df.drop(index=3).reset_index(drop=True)
        audit_df = audit_df.drop(index=3).reset_index(drop=True)

        with self.assertRaisesRegex(ValueError, "chưa liên tục"):
            merge_hourly_with_audit(hourly_df, audit_df)

    def test_calendar_features_are_created(self):
        hourly_df, audit_df = make_hourly_data()

        feature_df, _ = create_time_series_features(
            hourly_df,
            audit_df,
            lag_hours=[1],
            rolling_windows=[3],
        )

        self.assertIn("hour_sin", feature_df.columns)
        self.assertIn("day_of_week_cos", feature_df.columns)
        self.assertIn("is_weekend", feature_df.columns)

    def test_output_files_are_created(self):
        hourly_df, audit_df = make_hourly_data()
        feature_df, report = create_time_series_features(
            hourly_df,
            audit_df,
            lag_hours=[1],
            rolling_windows=[3],
        )

        with tempfile.TemporaryDirectory() as directory:
            csv_path = os.path.join(directory, "features.csv")
            report_path = os.path.join(directory, "report.json")
            save_time_series_features(
                feature_df,
                report,
                csv_path,
                report_path,
            )

            saved_df = pd.read_csv(csv_path)
            with open(report_path, "r", encoding="utf-8") as file:
                saved_report = json.load(file)

            self.assertEqual(len(saved_df), len(feature_df))
            self.assertEqual(
                saved_report["output_training_rows"],
                len(feature_df),
            )


if __name__ == "__main__":
    unittest.main()
