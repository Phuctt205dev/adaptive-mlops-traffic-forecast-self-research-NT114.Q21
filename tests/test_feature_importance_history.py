"""Historical averages must not mix raw SHAP scales or hide missing reports."""
import copy
import hashlib
import json
from types import SimpleNamespace
import uuid
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.db.models import Base, Dataset, ModelVersion, Region, TrainingRun, UserRole
from backend.app.api.dependencies import get_current_user
from backend.app.api.routes.feature_importance import router
from backend.app.db.session import get_db
from backend.app.schemas.feature_importance import FeatureImportanceReport
from backend.app.services import feature_importance as service
from tests import test_feature_importance as fixtures


class FeatureImportanceHistoryTests(unittest.TestCase):
    def setUp(self):
        fixtures.FeatureImportanceTests.setUp(self)
        self.rows = []
        self.db.execute.side_effect = lambda statement: iter(self.rows)
        self.objects = {(Region, self.region.id): self.region, (ModelVersion, self.model.id): self.model,
                        (TrainingRun, self.run.id): self.run, (Dataset, self.dataset.id): self.dataset}
        self.db.get.side_effect = lambda cls, key: self.objects.get((cls, key))

    def add(self, importance, name="RandomForest", dataset=None):
        dataset = dataset or self.dataset
        run = SimpleNamespace(id=uuid.uuid4(), region_id=self.region.id, dataset_id=dataset.id, configuration_json={})
        model = SimpleNamespace(id=uuid.uuid4(), region_id=self.region.id, training_run_id=run.id,
                                version=f"model_v{len(self.rows) + 1}", variant=name)
        if importance is not None:
            report = copy.deepcopy(self.report)
            report.update(model_name=name, importance=[{"feature": key, "mean_abs_shap": value} for key, value in importance.items()])
            report["context"]["dataset_id"] = str(dataset.id)
            if name in {"LSTM", "GRU"}:
                report.update(explainer="GradientExplainer", approximate=True, sequence_length=6,
                              aggregation="mean_samples_sum_timesteps_abs_shap", output_units="traffic_volume")
                report["context"].update(training_run_id=str(run.id), preprocessor_sha256="b" * 64)
                run.configuration_json.update(preprocessor_file="models/pre.pkl", recurrent_sequence_length=6)
            raw = json.dumps(report).encode()
            path = self.root / f"{model.id}.json"
            path.write_bytes(raw)
            run.configuration_json["shap_report"] = {"status": "completed", "source_report": str(path),
                "report_sha256": hashlib.sha256(raw).hexdigest(), "model_sha256": "a" * 64,
                "preprocessor_sha256": "b" * 64}
        for cls, obj in ((Dataset, dataset), (TrainingRun, run), (ModelVersion, model)):
            self.objects[cls, obj.id] = obj
        self.rows.append((model, run, dataset))
        return model, run, dataset

    def overview(self):
        result = service.get_feature_importance(self.db, self.region.id)
        FeatureImportanceReport.model_validate(result)
        self.db.commit.assert_not_called()
        return result

    def test_equal_version_weight_normalizes_before_averaging_and_groups_calendar(self):
        tree, _, _ = self.add({"hour_sin": 600, "hour_cos": 200, "temperature": 200})
        other = SimpleNamespace(id=uuid.uuid4(), region_id=self.region.id, original_filename="other.csv")
        self.add({"hour": 1, "temperature": 3}, name="LSTM", dataset=other)
        self.region.active_model_version_id = tree.id
        with patch.object(service, "report_bytes", wraps=service.report_bytes) as reader:
            result = self.overview()
        # The active report is reused, not fetched twice.
        self.assertEqual(reader.call_count, 2)
        history = result["history"]
        shares = {row["label"]: row["share_percent"] for row in history["features"]}
        self.assertEqual(shares, {"Time of day": 52.5, "Temperature": 47.5})
        self.assertEqual(history["dataset_count"], 2)
        self.assertEqual(history["used_reports"], 2)
        self.assertEqual(result["features"][0]["share_percent"], 80)

    def test_absent_group_is_distinct_from_missing_report_and_zero_report(self):
        self.region.active_model_version_id = None
        self.add({"temperature": 5})
        self.add({"humidity": 10}, name="GRU")
        self.add(None)
        self.add({"temperature": 0})
        result = self.overview()
        history = result["history"]
        self.assertEqual(history["used_reports"], 2)
        self.assertEqual(history["total_models"], 4)
        self.assertEqual(history["excluded_reports"]["missing"], 1)
        self.assertEqual(history["excluded_reports"]["zero"], 1)
        for row in history["features"]:
            self.assertEqual(row["share_percent"], 50)
            self.assertEqual(row["reports_present"], 1)
        self.assertIsNone(result["active_model_version_id"])

    def test_broken_active_report_does_not_hide_valid_history(self):
        active, run, _ = self.add({"temperature": 1})
        self.region.active_model_version_id = active.id
        run.configuration_json["shap_report"]["report_sha256"] = "0" * 64
        self.add({"humidity": 2}, "XGBoost")
        result = self.overview()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["history"]["used_reports"], 1)
        self.assertEqual(result["history"]["excluded_reports"]["invalid"], 1)

    def test_unavailable_invalid_unsupported_are_excluded_without_paths(self):
        self.region.active_model_version_id = None
        _, invalid, _ = self.add({"temperature": 2})
        invalid.configuration_json["shap_report"]["report_sha256"] = "0" * 64
        self.add(None, "Unknown")
        _, unavailable, _ = self.add(None)
        unavailable.configuration_json["shap_report"] = {"status": "disabled"}
        history = self.overview()["history"]
        self.assertEqual(history["status"], "pending")
        self.assertEqual(history["excluded_reports"], {"missing": 0, "unavailable": 1, "unsupported": 1, "invalid": 1, "zero": 0})
        self.assertNotIn(str(self.root), json.dumps(history, default=str))

    def test_foreign_dataset_or_run_never_contributes(self):
        self.region.active_model_version_id = None
        _, run, _ = self.add({"temperature": 5})
        run.region_id = uuid.uuid4()
        model, _, dataset = self.add({"humidity": 5})
        dataset.region_id = uuid.uuid4()
        history = self.overview()["history"]
        self.assertEqual(history["used_reports"], 0)
        self.assertEqual(history["excluded_reports"]["invalid"], 2)
        self.assertIsNone(history["models"][1]["dataset_filename"])

    def test_empty_region_and_active_change(self):
        self.region.active_model_version_id = None
        result = self.overview()
        self.assertEqual(result["history"]["total_models"], 0)
        self.assertEqual(result["history"]["status"], "pending")
        with self.assertRaises(service.ApplicationError) as failure:
            service.get_feature_importance(self.db, self.region.id, uuid.uuid4())
        self.assertEqual(failure.exception.code, "active_model_changed")

    def test_malformed_metadata_or_timestamp_is_excluded_without_breaking_overview(self):
        self.region.active_model_version_id = None
        _, run, _ = self.add(None)
        run.configuration_json["shap_report"] = ["corrupt"]
        model, dated, _ = self.add({"humidity": 1})
        path = self.root / f"{model.id}.json"
        report = json.loads(path.read_bytes())
        report["created_at"] = "not-a-date"
        raw = json.dumps(report).encode()
        path.write_bytes(raw)
        dated.configuration_json["shap_report"]["report_sha256"] = hashlib.sha256(raw).hexdigest()
        self.add({"temperature": 1})
        result = self.overview()["history"]
        self.assertEqual(result["used_reports"], 1)
        self.assertEqual(result["excluded_reports"]["invalid"], 2)

    def test_api_serializes_history_with_no_active_or_invalid_active(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1/admin")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role=UserRole.ADMIN)
        self.add({"temperature": 1})
        self.region.active_model_version_id = None
        with TestClient(app) as client:
            path = f"/api/v1/admin/regions/{self.region.id}/feature-importance"
            response = client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["active_model_version_id"])
            self.assertEqual(response.json()["history"]["used_reports"], 1)
            model, run, _ = self.add({"humidity": 2})
            self.region.active_model_version_id = model.id
            run.configuration_json["shap_report"]["report_sha256"] = "0" * 64
            response = client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "unavailable")
            self.assertEqual(response.json()["history"]["used_reports"], 1)
            self.assertNotIn("source_report", response.text)

    def test_sql_join_reads_only_region_versions_including_archived_and_orphans(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        first, second = uuid.uuid4(), uuid.uuid4()
        with engine.begin() as connection:
            for region_id in (first, second):
                connection.execute(ModelVersion.__table__.insert(), dict(id=uuid.uuid4(), region_id=region_id,
                    training_run_id=uuid.uuid4(), variant="RandomForest", version="model_v1", mlflow_run_id=str(region_id),
                    mlflow_model_uri="unused", artifact_uri="unused", cv_mean_mae=1, cv_std_mae=0, final_test_mae=1,
                    status="archived"))
        with Session(engine) as db:
            result = service.saved_model_history(db, SimpleNamespace(id=first, active_model_version_id=None), None)
        self.assertEqual(result["total_models"], 1)
        self.assertEqual(result["excluded_reports"]["invalid"], 1)
        engine.dispose()
