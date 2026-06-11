import unittest

import pandas as pd

from src.time_series_splits import (
    DEVELOPMENT_END,
    FINAL_TEST_START,
    OFFLINE_END,
    PRODUCTION_START,
    create_expanding_window_folds,
    split_development_and_final_test,
    split_offline_and_production,
)


def make_timeline():
    """Tạo nhãn theo ngày để test nhanh các ranh giới năm."""
    timestamps = pd.date_range(
        "2012-10-01",
        "2017-05-17",
        freq="D",
    )
    return pd.DataFrame(
        {
            "date_time": timestamps,
            "traffic_volume": range(len(timestamps)),
        }
    )


class TimeSeriesSplitTests(unittest.TestCase):
    def test_production_starts_in_2016_and_is_not_offline(self):
        offline, production = split_offline_and_production(
            make_timeline()
        )

        self.assertLessEqual(
            offline["date_time"].max(),
            OFFLINE_END,
        )
        self.assertGreaterEqual(
            production["date_time"].min(),
            PRODUCTION_START,
        )
        self.assertTrue(
            set(offline.index).isdisjoint(set(production.index))
        )

    def test_final_test_is_only_the_last_quarter_of_2015(self):
        development, final_test = (
            split_development_and_final_test(make_timeline())
        )

        self.assertLessEqual(
            development["date_time"].max(),
            DEVELOPMENT_END,
        )
        self.assertGreaterEqual(
            final_test["date_time"].min(),
            FINAL_TEST_START,
        )
        self.assertLessEqual(
            final_test["date_time"].max(),
            OFFLINE_END,
        )

    def test_expanding_folds_never_use_future_for_training(self):
        development, _ = split_development_and_final_test(
            make_timeline()
        )
        folds = create_expanding_window_folds(
            development,
            n_splits=5,
        )

        previous_train_rows = 0
        for fold in folds:
            train_df = fold["train"]
            validation_df = fold["validation"]
            self.assertGreater(
                len(train_df),
                previous_train_rows,
            )
            self.assertLess(
                train_df["date_time"].max(),
                validation_df["date_time"].min(),
            )
            self.assertLess(
                validation_df["date_time"].max(),
                FINAL_TEST_START,
            )
            previous_train_rows = len(train_df)


if __name__ == "__main__":
    unittest.main()
