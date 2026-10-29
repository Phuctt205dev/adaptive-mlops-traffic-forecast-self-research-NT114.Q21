"""Deletion regression with real foreign keys, isolated in-memory storage."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.errors import ApplicationError
from backend.app.db.models import (Base, Dataset, DatasetStatus, DriftCheck, DriftCheckStatus,
                                   ForecastMode, ModelVersion, Prediction, Region, TrainingRun, User, UserRole)
from backend.app.services.model_registry import activate_model_version, delete_model_version, register_training_result


class ModelDeletionConstraintTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _): connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.user = User(email="synthetic@example.test", password_hash="synthetic", role=UserRole.ADMIN)
        self.db.add(self.user); self.db.flush()

    def city(self, name):
        region = Region(name=name, latitude="10", longitude="106", timezone="Asia/Ho_Chi_Minh")
        self.db.add(region); self.db.flush()
        dataset = Dataset(region_id=region.id, contract_version="traffic-training-csv/v1", original_filename="synthetic.csv",
                          storage_uri="s3://synthetic-only/input.csv", sha256="a" * 64, row_count=5000,
                          status=DatasetStatus.VALID, uploaded_by=self.user.id)
        self.db.add(dataset); self.db.commit()
        return region, dataset

    def model(self, region, dataset, version="model_v1", variant="RandomForest"):
        return register_training_result(self.db, region.id, dataset.id, {
            "best_model_name": variant, "model_version": version, "validation_MAE": 1, "test_MAE": 2,
            "model_file": str(self.root / (version + ".pkl")), "versioned_model_file": str(self.root / (version + ".pkl")),
            "mlflow_run_id": str(uuid.uuid4()), "mlflow_model_uri": "synthetic://model"})

    def history(self, region, dataset, model):
        record = DriftCheck(region_id=region.id, dataset_id=dataset.id, active_model_version_id=model.id,
                            status=DriftCheckStatus.DRIFT_DETECTED, drift_detected=True, feature_count=2,
                            drifted_feature_count=1, feature_drift_json={"synthetic": {"score": .3}})
        prediction = Prediction(user_id=self.user.id, region_id=region.id, model_version_id=model.id,
                                forecast_for=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                forecast_mode=ForecastMode.NEXT_HOUR, prediction=123, feature_snapshot_json={})
        self.db.add_all([record, prediction]); self.db.commit()
        return record, prediction.id

    def test_active_model_with_drift_history_can_be_deleted_atomically(self):
        region, dataset = self.city("Second city")
        run, model = self.model(region, dataset, variant="GRU")
        activate_model_version(self.db, model.id)
        history, prediction_id = self.history(region, dataset, model)
        model_id = model.id
        delete_model_version(self.db, model_id)
        self.assertIsNone(self.db.get(ModelVersion, model_id))
        self.assertIsNone(self.db.get(Region, region.id).active_model_version_id)
        self.assertIsNone(self.db.get(TrainingRun, run.id).recommended_model_version_id)
        self.assertIsNone(self.db.get(Prediction, prediction_id))
        self.db.refresh(history)
        self.assertIsNone(history.active_model_version_id)
        self.assertTrue(history.drift_detected)
        self.assertEqual(history.feature_drift_json, {"synthetic": {"score": .3}})

    def test_archived_model_deletion_preserves_active_and_other_city(self):
        first, first_data = self.city("First city")
        _, protected = self.model(first, first_data)
        activate_model_version(self.db, protected.id)
        first_history, first_prediction = self.history(first, first_data, protected)
        second, data = self.city("Second city")
        run, old = self.model(second, data)
        old_history, old_prediction = self.history(second, data, old)
        _, active = self.model(second, data, "model_v2", "GRU")
        activate_model_version(self.db, old.id)
        activate_model_version(self.db, active.id)
        old_id = old.id
        delete_model_version(self.db, old_id)
        self.assertIsNone(self.db.get(ModelVersion, old_id))
        self.assertEqual(self.db.get(Region, second.id).active_model_version_id, active.id)
        self.assertEqual(self.db.get(Region, first.id).active_model_version_id, protected.id)
        self.assertIsNotNone(self.db.get(Prediction, first_prediction))
        self.assertIsNone(self.db.get(Prediction, old_prediction))
        self.db.refresh(first_history); self.db.refresh(old_history)
        self.assertEqual(first_history.active_model_version_id, protected.id)
        self.assertIsNone(old_history.active_model_version_id)

    def test_deletion_without_drift_works_in_any_city(self):
        for index in range(3):
            region, dataset = self.city("City " + str(index))
            _, model = self.model(region, dataset)
            activate_model_version(self.db, model.id)
            model_id = model.id
            delete_model_version(self.db, model_id)
            self.assertIsNone(self.db.get(ModelVersion, model_id))
            self.assertIsNone(self.db.get(Region, region.id).active_model_version_id)

    def test_conflict_rolls_back_every_change_and_returns_readable_error(self):
        region, dataset = self.city("Conflict city")
        run, model = self.model(region, dataset)
        activate_model_version(self.db, model.id)
        history, prediction_id = self.history(region, dataset, model)
        model_id = model.id
        with patch.object(self.db, "commit", side_effect=IntegrityError("synthetic", {}, Exception())), \
                self.assertRaises(ApplicationError) as failure:
            delete_model_version(self.db, model_id)
        self.assertEqual(failure.exception.code, "model_delete_conflict")
        self.assertIsNotNone(self.db.get(ModelVersion, model_id))
        self.assertEqual(self.db.get(Region, region.id).active_model_version_id, model_id)
        self.assertEqual(self.db.get(TrainingRun, run.id).recommended_model_version_id, model_id)
        self.assertIsNotNone(self.db.get(Prediction, prediction_id))
        self.db.refresh(history)
        self.assertEqual(history.active_model_version_id, model_id)


class ModelDeleteFixScopeTests(unittest.TestCase):
    def test_rollout_only_replaces_the_cloud_code_volume(self):
        from scripts.maintenance.model_delete_fix import source_patch
        volume = {"name": "cloud-code", "configMap": {"name": "before", "defaultMode": 420, "items": []}}
        result = source_patch(volume, {"metadata": {"name": "after"}}, [{"key": "a", "path": "b"}])
        self.assertEqual(set(result["spec"]["template"]["spec"]), {"volumes"})
        self.assertEqual(result["spec"]["template"]["spec"]["volumes"][0], {
            "name": "cloud-code", "configMap": {"name": "after", "defaultMode": 420, "items": [{"key": "a", "path": "b"}]}})
        self.assertEqual(volume["configMap"]["name"], "before")


if __name__ == "__main__":
    unittest.main()
