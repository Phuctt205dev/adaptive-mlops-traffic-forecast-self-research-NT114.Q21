import os
import tempfile
import unittest
from unittest.mock import patch

import joblib
import pandas as pd

import retrain_job
from retrain_job import (
    calculate_improvement_ratio,
    evaluate_pending_candidate,
    should_promote_candidate,
)
from src.drift import get_historical_mae_baseline


class ConstantModel:
    def __init__(self, value):
        self.value = value

    def predict(self, features):
        return [self.value] * len(features)


class HistoricalBaselineTests(unittest.TestCase):
    def test_uses_recent_median_for_the_same_model(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = os.path.join(directory, "drift_history.csv")
            pd.DataFrame(
                [
                    {
                        "model_version": "model_v1",
                        "current_mae": 100,
                        "drift": False,
                    },
                    {
                        "model_version": "model_v2",
                        "current_mae": 900,
                        "drift": False,
                    },
                    {
                        "model_version": "model_v1",
                        "current_mae": 120,
                        "drift": False,
                    },
                    {
                        "model_version": "model_v1",
                        "current_mae": 500,
                        "drift": True,
                    },
                    {
                        "model_version": "model_v1",
                        "current_mae": 110,
                        "drift": False,
                    },
                ]
            ).to_csv(log_path, index=False)

            baseline = get_historical_mae_baseline(
                model_version="model_v1",
                history_size=3,
                minimum_windows=3,
                log_path=log_path,
            )

            self.assertEqual(baseline, 110.0)

    def test_returns_none_until_enough_windows_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = os.path.join(directory, "drift_history.csv")
            pd.DataFrame(
                [
                    {"model_version": "model_v1", "current_mae": 100},
                    {"model_version": "model_v1", "current_mae": 120},
                ]
            ).to_csv(log_path, index=False)

            baseline = get_historical_mae_baseline(
                model_version="model_v1",
                history_size=6,
                minimum_windows=3,
                log_path=log_path,
            )

            self.assertIsNone(baseline)


class PromotionRuleTests(unittest.TestCase):
    def test_promotes_when_candidate_improves_by_five_percent(self):
        promote, improvement = should_promote_candidate(
            champion_mae=500,
            candidate_mae=470,
            minimum_improvement=0.05,
        )

        self.assertTrue(promote)
        self.assertAlmostEqual(improvement, 0.06)

    def test_rejects_candidate_when_improvement_is_too_small(self):
        promote, improvement = should_promote_candidate(
            champion_mae=500,
            candidate_mae=490,
            minimum_improvement=0.05,
        )

        self.assertFalse(promote)
        self.assertAlmostEqual(improvement, 0.02)

    def test_improvement_is_safe_when_champion_mae_is_zero(self):
        improvement = calculate_improvement_ratio(
            champion_mae=0,
            candidate_mae=0,
        )

        self.assertEqual(improvement, 0.0)

    def test_promoted_candidate_atomically_replaces_champion(self):
        with tempfile.TemporaryDirectory() as directory:
            champion_path = os.path.join(directory, "best_model.pkl")
            champion_info_path = os.path.join(directory, "best_info.json")
            candidate_path = os.path.join(directory, "candidate.pkl")
            candidate_info_path = os.path.join(
                directory,
                "candidate_info.json",
            )
            promotion_path = os.path.join(directory, "promotion.csv")

            champion = ConstantModel(80)
            candidate = ConstantModel(100)
            joblib.dump(champion, champion_path)
            joblib.dump(candidate, candidate_path)

            retrain_job.save_model_info(
                {"model_version": "model_v1"},
                champion_info_path,
            )
            retrain_job.save_model_info(
                {
                    "model_version": "model_v2",
                    "train_start_date": "2013-01-01",
                    "train_end_date": "2014-01-01",
                },
                candidate_info_path,
            )

            evaluation_window = pd.DataFrame(
                {
                    "date_time": pd.to_datetime(
                        ["2014-01-01", "2014-01-02"]
                    ),
                    "traffic_volume": [100, 100],
                    "feature": [1, 2],
                }
            )

            with patch.multiple(
                retrain_job,
                MODEL_PATH=champion_path,
                MODEL_INFO_PATH=champion_info_path,
                CANDIDATE_MODEL_PATH=candidate_path,
                CANDIDATE_INFO_PATH=candidate_info_path,
                PROMOTION_HISTORY_PATH=promotion_path,
            ):
                result = evaluate_pending_candidate(
                    champion,
                    evaluation_window,
                )

            promoted_model = joblib.load(champion_path)
            self.assertTrue(result["promoted"])
            self.assertEqual(promoted_model.value, 100)
            self.assertFalse(os.path.exists(candidate_path))
            self.assertTrue(os.path.exists(promotion_path))


if __name__ == "__main__":
    unittest.main()