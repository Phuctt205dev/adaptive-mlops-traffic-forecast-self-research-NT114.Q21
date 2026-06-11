import json
import os
import tempfile
import unittest

import pandas as pd

from src.time_series_preprocess import (
    METADATA_COLUMNS,
    ORIGINAL_COLUMN_ORDER,
    _build_seasonal_candidate_cache,
    _estimate_target,
    prepare_hourly_time_series,
    save_hourly_dataset,
)


def make_row(date_time, traffic_volume, weather_type="Clear"):
    """Tạo một dòng dữ liệu nhỏ dành riêng cho unit test."""
    return {
        "date_time": date_time,
        "is_holiday": None,
        "air_pollution_index": 100,
        "humidity": 60,
        "wind_speed": 5,
        "wind_direction": 180,
        "visibility_in_miles": 10,
        "dew_point": 10,
        "temperature": 280,
        "rain_p_h": 0,
        "snow_p_h": 0,
        "clouds_all": 20,
        "weather_type": weather_type,
        "weather_description": "clear sky",
        "traffic_volume": traffic_volume,
    }


class HourlyTimeSeriesPreparationTests(unittest.TestCase):
    def test_duplicate_hours_are_aggregated(self):
        raw_df = pd.DataFrame(
            [
                make_row("2024-01-01 00:00:00", 100, "Clear"),
                make_row("2024-01-01 00:00:00", 100, "Rain"),
                make_row("2024-01-01 01:00:00", 200, "Clear"),
            ]
        )

        hourly_df, audit_df, report = prepare_hourly_time_series(raw_df)
        first_hour = hourly_df.iloc[0]
        first_hour_audit = audit_df.iloc[0]

        self.assertEqual(len(hourly_df), 2)
        self.assertEqual(first_hour["traffic_volume"], 100)
        self.assertEqual(first_hour_audit["source_row_count"], 2)
        self.assertEqual(report["duplicate_extra_rows"], 1)

    def test_short_gap_is_interpolated_and_flagged(self):
        first = make_row("2024-01-01 00:00:00", 100)
        last = make_row("2024-01-01 02:00:00", 300)
        first["temperature"] = 280
        last["temperature"] = 284

        hourly_df, audit_df, _ = prepare_hourly_time_series(
            pd.DataFrame([first, last])
        )
        missing_hour = hourly_df.iloc[1]
        missing_hour_audit = audit_df.iloc[1]

        self.assertEqual(missing_hour["temperature"], 282)
        self.assertEqual(missing_hour["traffic_volume"], 200)
        self.assertFalse(missing_hour_audit["is_observed_hour"])
        self.assertFalse(missing_hour_audit["target_observed"])
        self.assertTrue(missing_hour_audit["target_is_imputed"])
        self.assertEqual(
            missing_hour_audit["target_imputation_method"],
            "short_gap_linear",
        )

    def test_seasonal_target_uses_same_hour_and_weekday(self):
        timestamps = pd.date_range(
            "2024-01-01 08:00:00",
            periods=24 * 15,
            freq="h",
        )
        rows = []
        missing_time = pd.Timestamp("2024-01-08 08:00:00")

        for timestamp in timestamps:
            if timestamp == missing_time:
                continue
            traffic = 1000 + timestamp.hour * 100
            if timestamp.dayofweek < 5:
                traffic += 500
            rows.append(make_row(timestamp, traffic))

        hourly_df, audit_df, _ = prepare_hourly_time_series(
            pd.DataFrame(rows)
        )
        imputed = hourly_df[
            hourly_df["date_time"] == missing_time
        ].iloc[0]

        self.assertEqual(imputed["traffic_volume"], 2300)
        imputed_audit = audit_df[
            audit_df["date_time"] == missing_time
        ].iloc[0]
        self.assertEqual(
            imputed_audit["target_imputation_method"],
            "seasonal_same_hour_weekday",
        )
        self.assertFalse(imputed_audit["target_observed"])

    def test_long_gap_does_not_use_linear_method(self):
        timestamps = pd.date_range(
            "2024-01-01",
            periods=24 * 21,
            freq="h",
        )
        gap_start = pd.Timestamp("2024-01-10 00:00:00")
        gap_end = pd.Timestamp("2024-01-10 11:00:00")
        rows = []

        for timestamp in timestamps:
            if gap_start <= timestamp <= gap_end:
                continue
            rows.append(
                make_row(
                    timestamp,
                    1000 + timestamp.hour * 10,
                )
            )

        _, audit_df, _ = prepare_hourly_time_series(
            pd.DataFrame(rows)
        )
        gap_rows = audit_df[
            (audit_df["date_time"] >= gap_start)
            & (audit_df["date_time"] <= gap_end)
        ]

        self.assertTrue(
            gap_rows["target_imputation_method"]
            .str.startswith("seasonal_")
            .all()
        )
        self.assertTrue(
            (
                gap_rows["gap_length_hours"]
                == 12
            ).all()
        )

    def test_very_long_gap_uses_climatology_from_other_years(self):
        # Các mốc đều là thứ Hai lúc 08:00 trong tháng 1, nhưng cách nhau
        # hơn 35 ngày nên không thể dùng dữ liệu mùa vụ lân cận.
        observed_df = pd.DataFrame(
            [
                make_row("2024-01-01 08:00:00", 1000),
                make_row("2026-01-05 08:00:00", 3000),
            ]
        ).set_index("date_time")
        observed_df.index = pd.to_datetime(observed_df.index)

        missing_time = pd.Timestamp("2025-01-06 08:00:00")
        completed_df = pd.DataFrame(
            {"gap_length_hours": [24 * 365]},
            index=[missing_time],
        )
        candidate_cache = _build_seasonal_candidate_cache(
            observed_df
        )

        value, method, confidence = _estimate_target(
            completed_df,
            observed_df,
            missing_time,
            candidate_cache=candidate_cache,
        )

        self.assertGreater(value, 1000)
        self.assertLess(value, 3000)
        self.assertEqual(
            method,
            "climatology_month_hour_weekday",
        )
        self.assertGreater(confidence, 0)

    def test_numeric_output_is_rounded_like_source(self):
        first = make_row("2024-01-01 00:00:00", 101)
        last = make_row("2024-01-01 02:00:00", 200)
        first["humidity"] = 55
        last["humidity"] = 56
        first["temperature"] = 280.11
        last["temperature"] = 281.22

        hourly_df, _, _ = prepare_hourly_time_series(
            pd.DataFrame([first, last])
        )
        imputed = hourly_df.iloc[1]

        self.assertEqual(imputed["humidity"], 56)
        self.assertEqual(
            imputed["temperature"],
            round(imputed["temperature"], 2),
        )

    def test_target_conflict_is_reported(self):
        raw_df = pd.DataFrame(
            [
                make_row("2024-01-01 00:00:00", 100),
                make_row("2024-01-01 00:00:00", 200),
                make_row("2024-01-01 01:00:00", 300),
            ]
        )

        hourly_df, _, report = prepare_hourly_time_series(raw_df)

        self.assertEqual(hourly_df.iloc[0]["traffic_volume"], 150)
        self.assertEqual(
            report["duplicate_hours_with_target_conflict"],
            1,
        )

    def test_output_files_are_created(self):
        raw_df = pd.DataFrame(
            [
                make_row("2024-01-01 00:00:00", 100),
                make_row("2024-01-01 01:00:00", 200),
            ]
        )
        hourly_df, audit_df, report = prepare_hourly_time_series(raw_df)

        with tempfile.TemporaryDirectory() as directory:
            csv_path = os.path.join(directory, "hourly.csv")
            audit_path = os.path.join(directory, "hourly_audit.csv")
            report_path = os.path.join(directory, "report.json")

            save_hourly_dataset(
                hourly_df,
                audit_df,
                report,
                csv_path,
                audit_path,
                report_path,
            )

            saved_df = pd.read_csv(csv_path)
            saved_audit_df = pd.read_csv(audit_path)
            with open(
                report_path,
                "r",
                encoding="utf-8",
            ) as file:
                saved_report = json.load(file)

            self.assertEqual(len(saved_df), 2)
            self.assertEqual(
                list(saved_df.columns),
                ORIGINAL_COLUMN_ORDER,
            )
            self.assertEqual(
                list(saved_audit_df.columns),
                ["date_time"] + METADATA_COLUMNS,
            )
            self.assertEqual(saved_report["frequency"], "1h")

    def test_main_csv_has_exactly_the_original_schema(self):
        raw_df = pd.DataFrame(
            [
                make_row("2024-01-01 00:00:00", 100),
                make_row("2024-01-01 01:00:00", 200),
            ]
        )

        hourly_df, audit_df, _ = prepare_hourly_time_series(raw_df)

        self.assertEqual(list(hourly_df.columns), ORIGINAL_COLUMN_ORDER)
        self.assertEqual(
            list(audit_df.columns),
            ["date_time"] + METADATA_COLUMNS,
        )
        self.assertTrue(
            set(METADATA_COLUMNS).isdisjoint(hourly_df.columns)
        )

    def test_holiday_name_is_preserved(self):
        holiday = make_row("2024-01-01 00:00:00", 100)
        holiday["is_holiday"] = "New Years Day"
        normal = make_row("2024-01-01 01:00:00", 200)

        hourly_df, _, _ = prepare_hourly_time_series(
            pd.DataFrame([holiday, normal])
        )

        self.assertEqual(
            hourly_df.iloc[0]["is_holiday"],
            "New Years Day",
        )
        self.assertTrue(pd.isna(hourly_df.iloc[1]["is_holiday"]))


if __name__ == "__main__":
    unittest.main()
