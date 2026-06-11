import unittest

import numpy as np

from src.inference import prepare_input


class DummyModel:
    """Model giả chỉ cung cấp danh sách feature mà inference cần căn chỉnh."""

    feature_names_in_ = np.array(
        [
            "air_pollution_index",
            "humidity",
            "hour",
            "weather_type_Clouds",
        ]
    )


class InferencePreparationTests(unittest.TestCase):
    def test_prepare_input_aligns_columns_with_model(self):
        raw_input = {
            "date_time": "2013-12-01 08:00:00",
            "is_holiday": None,
            "air_pollution_index": 121,
            "humidity": 89,
            "wind_speed": 2,
            "wind_direction": 329,
            "visibility_in_miles": 1,
            "dew_point": 1,
            "temperature": 40,
            "rain_p_h": 0,
            "snow_p_h": 0,
            "clouds_all": 40,
            "weather_type": "Clouds",
            "weather_description": "scattered clouds",
        }

        prepared = prepare_input(raw_input, DummyModel())

        self.assertEqual(
            list(prepared.columns),
            list(DummyModel.feature_names_in_),
        )
        self.assertEqual(prepared.iloc[0]["hour"], 8)
        self.assertEqual(
            prepared.iloc[0]["weather_type_Clouds"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
