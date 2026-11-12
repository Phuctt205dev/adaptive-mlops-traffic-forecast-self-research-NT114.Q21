"""Average only models trained in one activated run, never historical versions."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.app.api.dependencies import get_current_user
from backend.app.api.routes.feature_importance import router
from backend.app.db.models import UserRole, DriftCheck, DriftCheckStatus
from backend.app.db.session import get_db
from backend.app.schemas.feature_importance import FeatureImportanceReport
from backend.app.services import feature_importance as service
from tests import test_feature_importance as fixtures


class FeatureImportanceHistoryTests(unittest.TestCase):
    def setUp(self):
        fixtures.FeatureImportanceTests.setUp(self)
        self.run.configuration_json["shap_reports"] = {}

    def add_report(self, name, values, *, context_run=None):
        report = copy.deepcopy(self.report)
        report.update(model_name=name, importance=[{"feature": key, "mean_abs_shap": value} for key, value in values.items()])
        report["context"]["training_run_id"] = str(context_run or self.run.id)
        if name in {"LSTM", "GRU"}:
            report.update(explainer="GradientExplainer", approximate=True, sequence_length=6,
                aggregation="mean_samples_sum_timesteps_abs_shap", output_units="traffic_volume")
            report["context"]["preprocessor_sha256"] = "b" * 64
        raw = json.dumps(report).encode()
        path = self.root / f"{name}.json"
        path.write_bytes(raw)
        metadata = {"status": "completed", "source_report": str(path), "report_sha256": hashlib.sha256(raw).hexdigest(),
                    "model_sha256": "a" * 64, "input_features": list(values)}
        if name in {"LSTM", "GRU"}:
            metadata.update(preprocessor_sha256="b" * 64, preprocessor_file="models/preprocessor.pkl", sequence_length=6)
        self.run.configuration_json["shap_reports"][name] = metadata
        if name == self.model.variant:
            self.run.configuration_json["shap_report"] = metadata
        return metadata

    def overview(self):
        result = service.get_feature_importance(self.db, self.region.id)
        FeatureImportanceReport.model_validate(result)
        self.db.commit.assert_not_called()
        self.db.execute.assert_not_called()  # Never scan historical versions.
        return result

    def all_five(self):
        for name, values in zip(service.MODEL_NAMES, [(800, 200), (0, 3), (2, 0), (.1, .1), (2, 8)]):
            self.add_report(name, dict(zip(("temperature", "humidity"), values)))

    def test_equal_algorithm_weight_normalizes_scales_for_all_five(self):
        self.all_five()
        result = self.overview()
        average = result["model_average"]
        self.assertEqual(average["status"], "ready")
        self.assertEqual(average["used_reports"], 5)
        self.assertEqual({row["label"]: row["share_percent"] for row in average["features"]},
                         {"Temperature": 50, "Humidity": 50})
        self.assertEqual(len(result["model_reports"]), 5)
        self.assertEqual(result["features"][0]["share_percent"], 80)
        self.assertEqual([row["model_name"] for row in result["model_reports"] if row["is_serving"]], ["RandomForest"])

    def test_missing_report_for_a_selected_model_keeps_average_incomplete(self):
        self.run.configuration_json["selected_models"] = ["random_forest_lag", "gru"]
        self.add_report("RandomForest", {"temperature": 1})
        result = self.overview()
        self.assertEqual(result["model_average"]["status"], "incomplete")
        self.assertEqual(result["model_average"]["used_reports"], 1)
        self.assertEqual(result["model_average"]["total_models"], 2)
        self.assertEqual(result["model_average"]["features"], [])
        self.assertEqual(result["model_reports"][-1]["status"], "pending")
        self.assertEqual([row["model_name"] for row in result["model_reports"]], ["RandomForest", "GRU"])

    def test_average_and_details_follow_each_selected_cohort_from_one_to_five(self):
        self.all_five()
        selections = [("gru",), ("random_forest_lag", "lstm"),
                      ("random_forest_lag", "xgboost_lag", "lightgbm_lag"),
                      ("random_forest_lag", "xgboost_lag", "lstm", "gru"),
                      ("random_forest_lag", "xgboost_lag", "lightgbm_lag", "lstm", "gru")]
        display_names = {**service.TREE_LAG_VARIANTS, **service.RECURRENT_VARIANTS}
        temperature = {"RandomForest": 80, "XGBoost": 0, "LightGBM": 100, "LSTM": 50, "GRU": 20}
        for selection in selections:
            with self.subTest(selection=selection):
                self.run.configuration_json["selected_models"] = list(selection)
                self.model.variant = display_names[selection[0]]
                self.run.configuration_json["shap_report"] = self.run.configuration_json["shap_reports"][self.model.variant]
                if self.model.variant in {"LSTM", "GRU"}:
                    self.run.configuration_json.update(preprocessor_file="models/preprocessor.pkl", recurrent_sequence_length=6)
                result = self.overview()
                average = result["model_average"]
                expected_names = {display_names[key] for key in selection}
                self.assertEqual({row["model_name"] for row in result["model_reports"]}, expected_names)
                self.assertEqual(average["status"], "ready")
                self.assertEqual(average["used_reports"], len(selection))
                self.assertEqual(average["total_models"], len(selection))
                shares = {row["label"]: row["share_percent"] for row in average["features"]}
                self.assertAlmostEqual(shares["Temperature"], sum(temperature[name] for name in expected_names) / len(selection))
                self.assertAlmostEqual(sum(shares.values()), 100)

    def test_features_absent_from_a_model_contribute_zero_to_the_cohort_average(self):
        self.run.configuration_json["selected_models"] = ["random_forest_lag", "xgboost_lag", "gru"]
        self.add_report("RandomForest", {"temperature": 1000})
        self.add_report("XGBoost", {"humidity": 2})
        self.add_report("GRU", {"humidity": .1})
        average = self.overview()["model_average"]
        rows = {row["label"]: row for row in average["features"]}
        self.assertAlmostEqual(rows["Temperature"]["share_percent"], 100 / 3)
        self.assertEqual(rows["Temperature"]["reports_present"], 1)
        self.assertAlmostEqual(rows["Humidity"]["share_percent"], 200 / 3)
        self.assertEqual(rows["Humidity"]["reports_present"], 2)

    def test_duplicate_selections_do_not_duplicate_weight_or_options(self):
        self.all_five()
        self.run.configuration_json["selected_models"] = ["gru", "random_forest_lag", "gru"]
        result = self.overview()
        self.assertEqual([row["model_name"] for row in result["model_reports"]], ["RandomForest", "GRU"])
        self.assertEqual(result["model_average"]["total_models"], 2)

    def test_older_runs_infer_models_from_selection_comparison_or_saved_reports(self):
        self.add_report("RandomForest", {"temperature": 1})
        self.add_report("XGBoost", {"humidity": 1})
        self.add_report("GRU", {"humidity": 1})
        for evidence in ({}, {"selected_from_candidates": ["random_forest_lag", "xgboost_lag", "gru"]},
                         {"model_comparison": [{"model_name": "RandomForest"}, {"best_model_name": "XGBoost"},
                                               {"model_name": "GRU"}]}):
            with self.subTest(evidence=evidence):
                for key in ("selected_from_candidates", "model_comparison"):
                    self.run.configuration_json.pop(key, None)
                self.run.configuration_json.update(evidence)
                result = self.overview()
                self.assertEqual(result["model_average"]["status"], "ready")
                self.assertEqual(result["model_average"]["total_models"], 3)
                self.assertEqual(len(result["model_reports"]), 3)

    def test_older_comparison_preserves_a_trained_model_without_a_report(self):
        self.add_report("RandomForest", {"temperature": 1})
        self.run.configuration_json["model_comparison"] = [{"model_name": "RandomForest"}, {"model_name": "LSTM"}]
        result = self.overview()
        self.assertEqual(result["model_average"]["status"], "incomplete")
        self.assertEqual(result["model_average"]["total_models"], 2)
        self.assertEqual([row["model_name"] for row in result["model_reports"]], ["RandomForest", "LSTM"])

    def test_old_winner_report_remains_viewable_without_fabricating_other_models(self):
        fixtures.FeatureImportanceTests.save_report(self)
        self.run.configuration_json.pop("shap_reports")
        result = self.overview()
        self.assertEqual(result["model_reports"][0]["status"], "ready")
        self.assertEqual(result["model_average"]["used_reports"], 1)
        self.assertEqual(len(result["model_reports"]), 1)
        self.assertEqual(result["model_average"]["total_models"], 1)
        self.assertEqual(result["model_average"]["status"], "ready")

    def test_reports_from_another_run_dataset_or_algorithm_are_excluded(self):
        self.all_five()
        self.add_report("XGBoost", {"temperature": 1}, context_run=uuid.uuid4())
        metadata = self.run.configuration_json["shap_reports"]["GRU"]
        path = Path(metadata["source_report"])
        report = json.loads(path.read_bytes())
        report["context"]["dataset_id"] = str(uuid.uuid4())
        raw = json.dumps(report).encode()
        path.write_bytes(raw)
        metadata["report_sha256"] = hashlib.sha256(raw).hexdigest()
        result = self.overview()
        self.assertEqual(result["model_average"]["used_reports"], 3)
        self.assertEqual(result["model_average"]["features"], [])
        self.assertEqual(result["model_reports"][1]["status"], "unavailable")

    def test_bad_active_report_does_not_hide_other_algorithm_reports(self):
        self.all_five()
        self.run.configuration_json["shap_reports"]["RandomForest"]["report_sha256"] = "0" * 64
        result = self.overview()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["model_average"]["used_reports"], 4)
        self.assertEqual(result["model_reports"][-1]["status"], "ready")

    def test_zero_report_does_not_silently_inflate_the_other_four(self):
        self.all_five()
        self.add_report("LightGBM", {"temperature": 0})
        result = self.overview()
        self.assertEqual(result["model_average"]["status"], "incomplete")
        self.assertEqual(result["model_average"]["used_reports"], 4)

    def test_no_active_model_does_not_borrow_an_unrelated_version(self):
        self.region.active_model_version_id = None
        result = self.overview()
        self.assertEqual(result["model_reports"], [])
        self.assertEqual(result["model_average"]["used_reports"], 0)
        self.assertEqual(result["model_average"]["total_models"], 0)

    def test_api_serializes_three_trained_models_with_dynamic_denominator(self):
        self.all_five()
        self.run.configuration_json["selected_models"] = ["random_forest_lag", "xgboost_lag", "lightgbm_lag"]
        response = self.api_response()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["model_average"]["total_models"], 3)
        self.assertEqual(body["model_average"]["used_reports"], 3)
        self.assertEqual(body["model_average"]["status"], "ready")
        self.assertEqual([row["model_name"] for row in body["model_reports"]], ["RandomForest", "XGBoost", "LightGBM"])

    def test_api_serializes_all_reports_without_paths_or_model_loading(self):
        self.all_five()
        response = self.api_response()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["model_reports"]), 5)
        self.assertNotIn("source_report", response.text)
        self.assertNotIn(str(self.root), response.text)

    def api_response(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1/admin")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role=UserRole.ADMIN)
        with TestClient(app) as client:
            response = client.get(f"/api/v1/admin/regions/{self.region.id}/feature-importance")
        return response

    def test_active_feature_membership_uses_inputs_including_zero_influence(self):
        self.run.configuration_json["model_input_features"] = ["temperature", "weather_type_Rain", "is_holiday_binary"]
        usage = service.active_model_feature_usage(self.db, self.region)
        self.assertTrue(service.raw_feature_is_used("weather_type", usage["input_features"]))
        self.assertTrue(service.raw_feature_is_used("is_holiday", usage["input_features"]))
        self.assertFalse(service.raw_feature_is_used("weather_description", usage["input_features"]))
        self.assertFalse(service.raw_feature_is_used("humidity", usage["input_features"]))
        self.assertIsNone(service.raw_feature_is_used("humidity", None))

    def test_drift_list_marks_active_model_without_changing_scores_or_detection(self):
        from backend.app.services import drift_monitoring
        self.run.configuration_json["model_input_features"] = ["temperature", "weather_type_Rain"]
        check = DriftCheck(id=uuid.uuid4(), region_id=self.region.id, active_model_version_id=self.model.id,
            dataset_id=self.dataset.id, status=DriftCheckStatus.DRIFT_DETECTED, drift_detected=True,
            drifted_feature_count=2, feature_count=2, created_at=datetime.now(timezone.utc),
            feature_drift_json={"features": {"temperature": {"drift": True}, "humidity": {"drift": True}}})
        before = copy.deepcopy(check.feature_drift_json)
        self.db.scalars.return_value = [check]
        with patch.object(drift_monitoring, "_prune_region_checks"):
            result = drift_monitoring.list_region_drift_checks(self.db, self.region.id)
        self.assertEqual(result["active_model_feature_usage"]["features"], {"temperature": True, "humidity": False})
        self.assertEqual(result["latest"]["feature_drift_json"], before)
        self.assertTrue(result["latest"]["drift_detected"])
