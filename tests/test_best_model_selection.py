import json
import os
import tempfile
import unittest

from src.best_model_selection import (
    rank_model_reports,
    select_and_save_best_model,
)
from src.training_variants import VARIANT_SPECS


def make_report(
    variant,
    artifact_path,
    cv_mae,
    cv_std,
    validation_end="2015-01-31T00:00:00",
):
    """Tạo báo cáo tối giản nhưng đúng hợp đồng selection."""
    return {
        "variant": variant,
        "model": variant,
        "family": "test_family",
        "cv_metrics": {
            "MAE": {"mean": cv_mae, "std": cv_std}
        },
        "final_test_metrics": {"MAE": cv_mae + 10},
        "split_policy": {"cv_splits": 2},
        "random_state": 42,
        "production_used_for_training": False,
        "artifact_paths": [artifact_path],
        "report_path": f"{variant}_report.json",
        "development": {
            "start": "2014-01-01T00:00:00",
            "end": "2015-09-30T23:00:00",
            "rows": 100,
        },
        "final_test": {
            "start": "2015-10-01T00:00:00",
            "end": "2015-12-31T23:00:00",
            "rows": 20,
        },
        "production_reserved": {
            "start": "2016-01-01T00:00:00",
            "end": "2017-01-01T00:00:00",
            "rows": 30,
        },
        "folds": [
            {
                "train": {
                    "start": "2014-01-01T00:00:00",
                    "end": "2014-06-30T00:00:00",
                    "rows": 40,
                },
                "validation": {
                    "start": "2014-07-01T00:00:00",
                    "end": validation_end,
                    "rows": 30,
                },
            },
            {
                "train": {
                    "start": "2014-01-01T00:00:00",
                    "end": validation_end,
                    "rows": 70,
                },
                "validation": {
                    "start": "2015-02-01T00:00:00",
                    "end": "2015-09-30T23:00:00",
                    "rows": 30,
                },
            },
        ],
    }


class BestModelSelectionTests(unittest.TestCase):
    def test_project_defines_exactly_eight_training_variants(self):
        self.assertEqual(len(VARIANT_SPECS), 8)
        self.assertEqual(
            set(VARIANT_SPECS),
            {
                "random_forest_no_lag",
                "random_forest_lag",
                "xgboost_no_lag",
                "xgboost_lag",
                "lightgbm_no_lag",
                "lightgbm_lag",
                "lstm",
                "gru",
            },
        )

    def test_ranking_uses_cv_mean_mae_before_final_test(self):
        with tempfile.TemporaryDirectory() as directory:
            first_artifact = os.path.join(directory, "first.pkl")
            second_artifact = os.path.join(directory, "second.pkl")
            open(first_artifact, "wb").close()
            open(second_artifact, "wb").close()
            first = make_report(
                "first",
                first_artifact,
                cv_mae=100,
                cv_std=20,
            )
            second = make_report(
                "second",
                second_artifact,
                cv_mae=110,
                cv_std=1,
            )
            second["final_test_metrics"]["MAE"] = 1

            ranked = rank_model_reports([second, first])

        self.assertEqual(ranked[0]["variant"], "first")

    def test_different_fold_windows_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = os.path.join(directory, "model.pkl")
            open(artifact, "wb").close()
            first = make_report("first", artifact, 100, 10)
            second = make_report(
                "second",
                artifact,
                90,
                10,
                validation_end="2015-02-01T00:00:00",
            )

            with self.assertRaisesRegex(ValueError, "không dùng cùng"):
                rank_model_reports([first, second])

    def test_selection_copies_winner_to_versioned_champion(self):
        with tempfile.TemporaryDirectory() as directory:
            report_paths = []
            for variant, mae in (("first", 100), ("second", 120)):
                artifact_path = os.path.join(
                    directory,
                    f"{variant}.pkl",
                )
                with open(artifact_path, "wb") as file:
                    file.write(variant.encode("ascii"))
                report = make_report(
                    variant,
                    artifact_path,
                    cv_mae=mae,
                    cv_std=5,
                )
                report_path = os.path.join(
                    directory,
                    f"{variant}.json",
                )
                report["report_path"] = report_path
                with open(
                    report_path,
                    "w",
                    encoding="utf-8",
                ) as file:
                    json.dump(report, file)
                report_paths.append(report_path)

            result = select_and_save_best_model(
                report_paths,
                ranking_path=os.path.join(
                    directory,
                    "ranking.csv",
                ),
                selection_report_path=os.path.join(
                    directory,
                    "selection.json",
                ),
                champion_root=os.path.join(
                    directory,
                    "champion",
                ),
            )

            self.assertEqual(result["selected_variant"], "first")
            self.assertTrue(
                os.path.exists(
                    result["champion"]["artifact_paths"][0]
                )
            )

    def test_partial_selection_can_skip_champion_save(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact_path = os.path.join(directory, "model.pkl")
            with open(artifact_path, "wb") as file:
                file.write(b"model")
            report = make_report(
                "only_one",
                artifact_path,
                cv_mae=100,
                cv_std=5,
            )
            report_path = os.path.join(directory, "report.json")
            report["report_path"] = report_path
            with open(report_path, "w", encoding="utf-8") as file:
                json.dump(report, file)

            result = select_and_save_best_model(
                [report_path],
                ranking_path=os.path.join(directory, "ranking.csv"),
                selection_report_path=os.path.join(
                    directory,
                    "selection.json",
                ),
                champion_root=os.path.join(directory, "champion"),
                save_winner_as_champion=False,
            )

        self.assertIsNone(result["champion"])
        self.assertFalse(result["selection_complete"])


if __name__ == "__main__":
    unittest.main()
