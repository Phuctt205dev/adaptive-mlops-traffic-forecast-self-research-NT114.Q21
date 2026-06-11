import os
import tempfile
import unittest

import pandas as pd

from src.preprocess import load_observed_target_data


class ObservedTargetLoadingTests(unittest.TestCase):
    def test_inferred_targets_are_not_loaded_for_training(self):
        data_df = pd.DataFrame(
            {
                "date_time": [
                    "2024-01-01 00:00:00",
                    "2024-01-01 01:00:00",
                    "2024-01-01 02:00:00",
                ],
                "traffic_volume": [100, 200, 300],
            }
        )
        audit_df = pd.DataFrame(
            {
                "date_time": [
                    "2024-01-01 00:00:00",
                    "2024-01-01 01:00:00",
                ],
                "target_observed": [True, False],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            data_path = os.path.join(directory, "data.csv")
            audit_path = os.path.join(directory, "audit.csv")
            data_df.to_csv(data_path, index=False)
            audit_df.to_csv(audit_path, index=False)

            loaded_df = load_observed_target_data(
                data_path,
                audit_path,
            )

        # Giờ 01:00 là nhãn suy luận nên bị bỏ; 02:00 chưa có trong audit
        # được xem là dữ liệu production mới và vẫn được giữ lại.
        self.assertEqual(
            loaded_df["date_time"].tolist(),
            [
                "2024-01-01 00:00:00",
                "2024-01-01 02:00:00",
            ],
        )


if __name__ == "__main__":
    unittest.main()
