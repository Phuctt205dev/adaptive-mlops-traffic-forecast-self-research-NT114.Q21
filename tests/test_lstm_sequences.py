import os
import tempfile
import unittest

import numpy as np
import pandas as pd

from src.lstm_sequences import (
    SEQUENCE_NUMERIC_COLUMNS,
    build_sequences,
    create_lstm_sequence_datasets,
    fit_sequence_preprocessors,
    get_eligible_target_indices,
    prepare_sequence_source,
    save_lstm_sequence_artifacts,
    split_target_indices,
    transform_sequence_source,
)
from src.time_series_preprocess import ORIGINAL_COLUMN_ORDER


def make_hourly_and_audit(periods=240):
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
            "air_pollution_index": np.arange(periods) + 100,
            "humidity": [60] * periods,
            "wind_speed": [5] * periods,
            "wind_direction": [180] * periods,
            "visibility_in_miles": [10] * periods,
            "dew_point": [10] * periods,
            "temperature": np.arange(periods) + 280.0,
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


class LstmSequenceTests(unittest.TestCase):
    def test_sequence_has_three_dimensions(self):
        hourly, audit = make_hourly_and_audit()

        datasets, _, _ = create_lstm_sequence_datasets(
            hourly,
            audit,
            sequence_length=24,
        )

        self.assertEqual(datasets["train"]["X"].ndim, 3)
        self.assertEqual(datasets["train"]["X"].shape[1], 24)

    def test_sequence_ends_one_hour_before_target(self):
        hourly, audit = make_hourly_and_audit()
        source = prepare_sequence_source(hourly, audit)
        indices = get_eligible_target_indices(
            source,
            sequence_length=24,
        )
        split_indices = split_target_indices(indices)
        preprocessors = fit_sequence_preprocessors(
            source,
            split_indices["train"],
        )
        transformed = transform_sequence_source(
            source,
            preprocessors,
        )
        target_index = split_indices["train"][0]

        X, _, _, timestamps = build_sequences(
            transformed,
            source,
            [target_index],
            preprocessors["target_scaler"],
            sequence_length=24,
        )
        causal_position = preprocessors[
            "sequence_feature_names"
        ].index("traffic_history_value")
        expected_last_value = transformed[
            target_index - 1,
            causal_position,
        ]

        self.assertEqual(
            X[0, -1, causal_position],
            expected_last_value,
        )
        self.assertEqual(
            pd.Timestamp(timestamps[0]),
            source.iloc[target_index]["date_time"],
        )

    def test_unobserved_target_is_not_used_as_label(self):
        hourly, audit = make_hourly_and_audit()
        audit.loc[200, "target_observed"] = False
        source = prepare_sequence_source(hourly, audit)

        indices = get_eligible_target_indices(
            source,
            sequence_length=24,
        )

        self.assertNotIn(200, set(indices))

    def test_current_target_does_not_change_its_own_sequence(self):
        hourly, audit = make_hourly_and_audit()
        changed = hourly.copy()
        target_row = 220
        changed.loc[target_row, "traffic_volume"] = 999999

        original_data, _, _ = create_lstm_sequence_datasets(
            hourly,
            audit,
            sequence_length=24,
        )
        changed_data, _, _ = create_lstm_sequence_datasets(
            changed,
            audit,
            sequence_length=24,
        )
        timestamp = np.datetime64(
            hourly.loc[target_row, "date_time"]
        )

        original_index = np.where(
            original_data["test"]["timestamps"] == timestamp
        )[0][0]
        changed_index = np.where(
            changed_data["test"]["timestamps"] == timestamp
        )[0][0]
        np.testing.assert_allclose(
            original_data["test"]["X"][original_index],
            changed_data["test"]["X"][changed_index],
        )

    def test_feature_scaler_is_fit_only_through_train_history(self):
        hourly, audit = make_hourly_and_audit()
        source = prepare_sequence_source(hourly, audit)
        indices = get_eligible_target_indices(
            source,
            sequence_length=24,
        )
        splits = split_target_indices(indices)
        preprocessors = fit_sequence_preprocessors(
            source,
            splits["train"],
        )
        last_train_history_index = splits["train"][-1] - 1
        expected_mean = source.iloc[
            : last_train_history_index + 1
        ][SEQUENCE_NUMERIC_COLUMNS].astype(float).mean()

        np.testing.assert_allclose(
            preprocessors["feature_scaler"].mean_,
            expected_mean.to_numpy(),
        )

    def test_target_scaler_can_restore_original_target(self):
        hourly, audit = make_hourly_and_audit()
        datasets, preprocessors, _ = (
            create_lstm_sequence_datasets(
                hourly,
                audit,
                sequence_length=24,
            )
        )

        restored = preprocessors[
            "target_scaler"
        ].inverse_transform(datasets["test"]["y"])
        np.testing.assert_allclose(
            restored,
            datasets["test"]["raw_y"],
            rtol=1e-5,
        )

    def test_artifacts_are_saved(self):
        hourly, audit = make_hourly_and_audit()
        datasets, preprocessors, report = (
            create_lstm_sequence_datasets(
                hourly,
                audit,
                sequence_length=24,
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            npz_path = os.path.join(directory, "sequences.npz")
            preprocessor_path = os.path.join(
                directory,
                "preprocessors.pkl",
            )
            report_path = os.path.join(directory, "report.json")
            save_lstm_sequence_artifacts(
                datasets,
                preprocessors,
                report,
                npz_path,
                preprocessor_path,
                report_path,
            )

            with np.load(npz_path) as saved:
                self.assertIn("X_train", saved.files)
            self.assertTrue(os.path.exists(preprocessor_path))
            self.assertTrue(os.path.exists(report_path))


if __name__ == "__main__":
    unittest.main()
