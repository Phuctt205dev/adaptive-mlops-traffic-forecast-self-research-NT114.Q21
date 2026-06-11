import unittest

from lightgbm import LGBMRegressor
from sklearn.ensemble import RandomForestRegressor
from xgboost import XGBRegressor

from src.models import (
    AUTOREGRESSIVE_MODEL_NAMES,
    build_autoregressive_model,
    build_original_model,
)


class ModelRegistryTests(unittest.TestCase):
    def test_autoregressive_profile_contains_all_three_models(self):
        self.assertEqual(
            set(AUTOREGRESSIVE_MODEL_NAMES),
            {"RandomForest", "LightGBM", "XGBoost"},
        )

    def test_original_profile_builds_three_tree_models(self):
        self.assertIsInstance(
            build_original_model("RandomForest", 42),
            RandomForestRegressor,
        )
        self.assertIsInstance(
            build_original_model("LightGBM", 42),
            LGBMRegressor,
        )
        self.assertIsInstance(
            build_original_model("XGBoost", 42),
            XGBRegressor,
        )

    def test_profiles_keep_different_xgboost_configuration(self):
        original = build_original_model("XGBoost", 42)
        autoregressive = build_autoregressive_model("XGBoost", 42)

        self.assertEqual(original.n_estimators, 300)
        self.assertEqual(autoregressive.n_estimators, 500)
        self.assertEqual(original.max_depth, 6)
        self.assertEqual(autoregressive.max_depth, 7)

    def test_unknown_model_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "không được hỗ trợ"):
            build_original_model("Unknown", 42)


if __name__ == "__main__":
    unittest.main()
