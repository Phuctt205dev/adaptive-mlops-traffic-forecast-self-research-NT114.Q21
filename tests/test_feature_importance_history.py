"""A five-model average is bound to one activated run, never version counts."""
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

    def test_equal_algorithm_weight_normalizes_scales_and_requires_all_five(self):
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

    def test_missing_report_never_becomes_a_partial_five_model_average(self):
        self.add_report("RandomForest", {"temperature": 1})
        result = self.overview()
        self.assertEqual(result["model_average"]["status"], "incomplete")
        self.assertEqual(result["model_average"]["used_reports"], 1)
        self.assertEqual(result["model_average"]["features"], [])
        self.assertEqual(result["model_reports"][-1]["status"], "pending")

    def test_old_winner_report_remains_viewable_without_fabricating_other_models(self):
        fixtures.FeatureImportanceTests.save_report(self)
        self.run.configuration_json.pop("shap_reports")
        result = self.overview()
        self.assertEqual(result["model_reports"][0]["status"], "ready")
        self.assertEqual(result["model_average"]["used_reports"], 1)
        self.assertEqual(len(result["model_reports"]), 5)

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

    def test_api_serializes_all_reports_without_paths_or_model_loading(self):
        self.all_five()
        app = FastAPI()
        app.include_router(router, prefix="/api/v1/admin")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role=UserRole.ADMIN)
        with TestClient(app) as client:
            response = client.get(f"/api/v1/admin/regions/{self.region.id}/feature-importance")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["model_reports"]), 5)
        self.assertNotIn("source_report", response.text)
        self.assertNotIn(str(self.root), response.text)

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
