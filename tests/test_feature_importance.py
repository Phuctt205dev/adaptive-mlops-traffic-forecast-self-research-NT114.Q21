import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.dependencies import get_current_user
from backend.app.api.routes.feature_importance import router
from backend.app.core.errors import ApplicationError, register_exception_handlers
from backend.app.db.models import Dataset, ModelVersion, Region, TrainingRun, UserRole
from backend.app.db.session import get_db
from backend.app.services import feature_importance as service
from backend.app.schemas.feature_importance import FeatureImportanceReport


class FeatureImportanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(service, "MODEL_ROOT", self.root).start()
        self.region = SimpleNamespace(id=uuid.uuid4(), active_model_version_id=uuid.uuid4())
        self.dataset = SimpleNamespace(id=uuid.uuid4(), region_id=self.region.id, original_filename="traffic.csv")
        self.run = SimpleNamespace(id=uuid.uuid4(), region_id=self.region.id, dataset_id=self.dataset.id, configuration_json={})
        self.model = SimpleNamespace(id=self.region.active_model_version_id, region_id=self.region.id,
                                     training_run_id=self.run.id, version="model_v1", variant="RandomForest")
        self.db = Mock()
        self.db.get.side_effect = lambda cls, key: {Region:self.region, ModelVersion:self.model, TrainingRun:self.run, Dataset:self.dataset}[cls]
        self.report = {"schema_version":1, "status":"completed", "explainer":"TreeExplainer",
                       "context":{"region_id":str(self.region.id),"dataset_id":str(self.dataset.id)},
                       "model_name":"RandomForest", "model_sha256":"a"*64, "final_test_used":False,
                       "sample_count":8, "created_at":"2026-10-27T03:00:00+00:00",
                       "importance":[{"feature":"hour","mean_abs_shap":20},
                                     {"feature":"hour_sin","mean_abs_shap":10},
                                     {"feature":"weather_type_Rain","mean_abs_shap":30},
                                     {"feature":"weather_type_Clear","mean_abs_shap":10},
                                     {"feature":"temperature","mean_abs_shap":30}]}

    def save_report(self):
        path = self.root / "model_shap.json"
        raw = json.dumps(self.report).encode()
        path.write_bytes(raw)
        self.run.configuration_json["shap_report"] = {
            "status":"completed", "source_report":str(path), "model_sha256":"a"*64,
            "report_sha256":hashlib.sha256(raw).hexdigest()}
        return path

    def read(self):
        return service.get_feature_importance(self.db, self.region.id, self.model.id)

    def test_groups_encoded_features_and_recomputes_shares(self):
        self.save_report()
        result = self.read()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["features"][0]["label"], "Weather conditions")
        self.assertEqual(result["features"][0]["share_percent"], 40)
        self.assertAlmostEqual(sum(row["share_percent"] for row in result["features"]), 100)
        self.assertEqual(result["transformed_feature_count"], 5)
        FeatureImportanceReport.model_validate(result)
        self.db.commit.assert_not_called()

    def test_model_without_report_is_pending_and_does_not_fetch_objects(self):
        with patch.object(service, "report_bytes") as read:
            self.assertEqual(self.read()["status"], "pending")
        read.assert_not_called()

    def test_neural_models_are_explicitly_for_later(self):
        self.model.variant = "LSTM"
        self.assertEqual(self.read()["status"], "unsupported")

    def test_zero_contribution_does_not_become_accuracy_or_nan(self):
        for row in self.report["importance"]: row["mean_abs_shap"] = 0
        self.save_report()
        self.assertEqual(sum(row["share_percent"] for row in self.read()["features"]), 0)

    def test_active_model_changed_is_rejected(self):
        with self.assertRaises(ApplicationError) as error:
            service.get_feature_importance(self.db, self.region.id, uuid.uuid4())
        self.assertEqual(error.exception.code, "active_model_changed")

    def test_cross_region_model_is_rejected(self):
        self.model.region_id = uuid.uuid4()
        with self.assertRaises(ApplicationError): self.read()

    def test_report_dataset_and_digest_must_match(self):
        for wrong in ("dataset", "region", "digest", "model", "nan"):
            with self.subTest(wrong=wrong):
                report = copy.deepcopy(self.report)
                if wrong == "dataset": self.report["context"]["dataset_id"] = str(uuid.uuid4())
                if wrong == "region": self.report["context"]["region_id"] = str(uuid.uuid4())
                if wrong == "model": self.report["model_name"] = "LightGBM"
                if wrong == "nan": self.report["importance"][0]["mean_abs_shap"] = float('nan')
                path = self.save_report()
                if wrong == "digest": path.write_text('{}')
                with self.assertRaises(ApplicationError) as error: self.read()
                self.assertEqual(error.exception.code, "shap_report_invalid")
                self.report = report

    def test_rejects_path_outside_models_and_oversized_report(self):
        with self.assertRaises(ValueError): service.report_bytes(str(self.root.parent / "private.json"))
        path = self.root / "large.json"
        path.write_bytes(b'x'*(service.MAX_REPORT_BYTES+1))
        with self.assertRaises(ValueError): service.report_bytes(str(path))

    def test_reads_saved_summary_from_minio_without_local_writes(self):
        path = self.root / "nested/report.json"
        raw = b'{"probe":true}'
        client = Mock()
        client.get_object.return_value = {"ContentLength":len(raw),"Body":io.BytesIO(raw)}
        with patch.dict(os.environ,{"TRAFFIC_API_RUNTIME_BUCKET":"test-runtime"}), patch.object(service.storage,"get_s3_client",return_value=client):
            self.assertEqual(service.report_bytes(str(path)), raw)
        client.get_object.assert_called_once_with(Bucket="test-runtime",Key="models/nested/report.json")
        self.assertFalse(path.exists())

    def test_endpoint_is_admin_only_and_returns_current_model(self):
        self.save_report()
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(router,prefix="/api/v1/admin")
        app.dependency_overrides[get_db] = lambda:self.db
        client = TestClient(app)
        path = f"/api/v1/admin/regions/{self.region.id}/feature-importance"
        self.assertEqual(client.get(path).status_code,401)
        app.dependency_overrides[get_current_user] = lambda:SimpleNamespace(role=UserRole.USER)
        self.assertEqual(client.get(path).status_code,403)
        app.dependency_overrides[get_current_user] = lambda:SimpleNamespace(role=UserRole.ADMIN)
        response=client.get(path)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()["features"][0]["label"],"Weather conditions")


if __name__ == '__main__': unittest.main()
