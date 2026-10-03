import unittest
from io import BytesIO

import pandas as pd

from backend.app.core.errors import ApplicationError
from backend.app.services.datasets import validate_training_csv


def build_csv(rows: int = 4380, **overrides) -> bytes:
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
            "traffic_volume": [820] * rows,
        }
    )
    for column, value in overrides.items():
        frame[column] = value
    return frame.to_csv(index=False).encode("utf-8")


class DatasetValidationTests(unittest.TestCase):
    def test_valid_training_csv_returns_metadata(self):
        result = validate_training_csv(build_csv())

        self.assertEqual(result.row_count, 4380)
        self.assertEqual(result.report["rows_raw"], 4380)
        self.assertEqual(result.report["duplicate_timestamps"], 0)

    def test_missing_required_column_is_rejected(self):
        frame = pd.read_csv(BytesIO(build_csv()))
        content = frame.drop(columns=["traffic_volume"]).to_csv(index=False).encode("utf-8")

        with self.assertRaises(ApplicationError) as context:
            validate_training_csv(content)

        self.assertEqual(context.exception.code, "dataset_schema_invalid")

    def test_invalid_numeric_range_is_rejected(self):
        with self.assertRaises(ApplicationError) as context:
            validate_training_csv(build_csv(humidity=[101] * 4380))

        self.assertEqual(context.exception.code, "dataset_range_invalid")


if __name__ == "__main__":
    unittest.main()
