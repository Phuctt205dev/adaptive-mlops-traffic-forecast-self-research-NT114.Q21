import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.errors import ApplicationError
from backend.app.db.models import (
    Base,
    Dataset,
    DatasetStatus,
    ModelVersion,
    ModelVersionStatus,
    Prediction,
    Region,
    TrainingRun,
    TrainingRunStatus,
    User,
    UserRole,
)
from backend.app.schemas.region import RegionCreate, RegionUpdate
from backend.app.services import regions


class RegionServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(cls.engine)

    def setUp(self):
        self.session = Session(self.engine)
        for table in reversed(Base.metadata.sorted_tables):
            self.session.execute(table.delete())
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def create_payload(self, name="Region A"):
        return RegionCreate(
            name=name,
            description="Test region",
            latitude="10.870000",
            longitude="106.803000",
            timezone="Asia/Ho_Chi_Minh",
        )

    def test_create_update_and_list_regions(self):
        created = regions.create_region(self.session, self.create_payload())
        updated = regions.update_region(
            self.session,
            created.id,
            RegionUpdate(is_active=False),
        )

        active_items, active_total = regions.list_regions(
            self.session, page=1, page_size=20, include_inactive=False
        )
        all_items, all_total = regions.list_regions(
            self.session, page=1, page_size=20, include_inactive=True
        )

        self.assertFalse(updated.is_active)
        self.assertEqual(active_items, [])
        self.assertEqual(active_total, 0)
        self.assertEqual(len(all_items), 1)
        self.assertEqual(all_total, 1)

    def test_duplicate_region_name_is_rejected(self):
        regions.create_region(self.session, self.create_payload())

        with self.assertRaises(ApplicationError) as context:
            regions.create_region(self.session, self.create_payload())

        self.assertEqual(context.exception.code, "region_name_conflict")
        self.assertEqual(context.exception.status_code, 409)

    def test_missing_region_is_rejected(self):
        import uuid

        with self.assertRaises(ApplicationError) as context:
            regions.get_region_or_404(self.session, uuid.uuid4())

        self.assertEqual(context.exception.code, "region_not_found")
        self.assertEqual(context.exception.status_code, 404)

    def test_delete_region_removes_related_records(self):
        import uuid
        from datetime import UTC, datetime

        user = User(email="admin@example.com", password_hash="hash", role=UserRole.ADMIN)
        region = regions.create_region(self.session, self.create_payload())
        self.session.add(user)
        self.session.flush()
        dataset = Dataset(
            region_id=region.id,
            contract_version="traffic-training-csv/v1",
            original_filename="traffic.csv",
            storage_uri="s3://traffic-datasets/traffic.csv",
            sha256="a" * 64,
            row_count=5000,
            status=DatasetStatus.VALID,
            uploaded_by=user.id,
        )
        self.session.add(dataset)
        self.session.flush()
        training_run = TrainingRun(
            region_id=region.id,
            dataset_id=dataset.id,
            status=TrainingRunStatus.COMPLETED,
            split_policy="time_series_cv_70_15_15",
            configuration_json={},
            requested_by=user.id,
        )
        self.session.add(training_run)
        self.session.flush()
        model_version = ModelVersion(
            region_id=region.id,
            training_run_id=training_run.id,
            variant="LightGBM",
            version="model_v1",
            mlflow_run_id=str(uuid.uuid4()),
            mlflow_model_uri="runs:/run/model",
            artifact_uri="models/model_v1.pkl",
            cv_mean_mae=1.0,
            cv_std_mae=0.1,
            final_test_mae=1.2,
            status=ModelVersionStatus.ACTIVE,
        )
        self.session.add(model_version)
        self.session.flush()
        training_run.recommended_model_version_id = model_version.id
        region.active_model_version_id = model_version.id
        prediction = Prediction(
            user_id=user.id,
            region_id=region.id,
            model_version_id=model_version.id,
            forecast_for=datetime(2026, 1, 1, tzinfo=UTC),
            forecast_mode="future_time",
            prediction=123.0,
            feature_snapshot_json={},
        )
        self.session.add(prediction)
        self.session.commit()

        backend = Mock()
        backend.barrier.return_value = nullcontext()
        backend.preflight.side_effect = lambda db, plan: plan
        with patch('backend.app.services.region_cleanup_resources.CleanupResources', return_value=backend):
            regions.delete_region(self.session, region.id)

        self.assertIsNone(self.session.get(Region, region.id))
        self.assertEqual(self.session.query(Dataset).count(), 0)
        self.assertEqual(self.session.query(TrainingRun).count(), 0)
        self.assertEqual(self.session.query(ModelVersion).count(), 0)
        self.assertEqual(self.session.query(Prediction).count(), 0)


if __name__ == "__main__":
    unittest.main()
