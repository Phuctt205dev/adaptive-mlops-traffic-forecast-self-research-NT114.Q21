import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.models import (
    Base,
    Dataset,
    DatasetStatus,
    ModelVersionStatus,
    Region,
    User,
    UserRole,
)
from backend.app.services.model_registry import (
    activate_model_version,
    create_queued_training_run,
    delete_model_version,
    list_region_model_versions,
    register_training_result,
)


class ModelRegistryServiceTests(unittest.TestCase):
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

    def seed_dataset(self):
        user = User(
            email="admin@example.com",
            password_hash="hash",
            role=UserRole.ADMIN,
        )
        region = Region(
            name="Region A",
            latitude="10.000000",
            longitude="106.000000",
            timezone="Asia/Ho_Chi_Minh",
        )
        self.session.add_all([user, region])
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
        self.session.commit()
        return user, region, dataset

    def model_info(self):
        return {
            "best_model_name": "LightGBM",
            "model_version": "model_v1",
            "validation_MAE": 296.7537,
            "validation_MAE_std": 12.34,
            "test_MAE": 239.9841,
            "train_start_date": "2012-10-02",
            "train_end_date": "2013-10-02",
            "cv_splits": 3,
            "random_state": 42,
            "data_version": "data_v1",
            "model_file": "models/candidate_model.pkl",
            "versioned_model_file": "models/model_v1.pkl",
            "mlflow_run_id": "run-1",
            "mlflow_model_uri": "runs:/run-1/LightGBM",
        }

    def test_register_and_activate_model_version(self):
        _user, region, dataset = self.seed_dataset()

        training_run, model_version = register_training_result(
            self.session,
            region.id,
            dataset.id,
            self.model_info(),
        )
        active = activate_model_version(self.session, model_version.id)
        items, total = list_region_model_versions(
            self.session,
            region.id,
            page=1,
            page_size=20,
        )
        refreshed_region = self.session.get(Region, region.id)

        self.assertEqual(training_run.recommended_model_version_id, model_version.id)
        self.assertEqual(model_version.variant, "LightGBM")
        self.assertEqual(model_version.cv_mean_mae, 296.7537)
        self.assertEqual(active.status, ModelVersionStatus.ACTIVE)
        self.assertEqual(refreshed_region.active_model_version_id, model_version.id)
        self.assertEqual(total, 1)
        self.assertEqual(items[0].id, model_version.id)

    def test_register_result_updates_existing_training_run(self):
        user, region, dataset = self.seed_dataset()
        queued = create_queued_training_run(
            self.session,
            dataset.id,
            user.id,
            {"train_start_date": "2012-10-02"},
            "manual-dag-run",
        )

        training_run, model_version = register_training_result(
            self.session,
            region.id,
            dataset.id,
            self.model_info(),
            training_run_id=queued.id,
        )

        self.assertEqual(training_run.id, queued.id)
        self.assertEqual(training_run.status.value, "completed")
        self.assertEqual(training_run.recommended_model_version_id, model_version.id)

    def test_delete_model_version_clears_active_region_model(self):
        _user, region, dataset = self.seed_dataset()
        _training_run, model_version = register_training_result(
            self.session,
            region.id,
            dataset.id,
            self.model_info(),
        )
        activate_model_version(self.session, model_version.id)

        delete_model_version(self.session, model_version.id)

        refreshed_region = self.session.get(Region, region.id)
        items, total = list_region_model_versions(
            self.session,
            region.id,
            page=1,
            page_size=20,
        )
        self.assertIsNone(refreshed_region.active_model_version_id)
        self.assertEqual(total, 0)
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
