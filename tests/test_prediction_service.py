import os
import tempfile
import unittest
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import patch

import joblib
import numpy as np
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
from backend.app.schemas.prediction import PredictionCreate
from backend.app.services.predictions import get_prediction_window, predict_for_region


class DummyPredictionModel:
    feature_names_in_ = np.array(
        [
            "air_pollution_index",
            "humidity",
            "hour",
            "weather_type_Rain",
        ]
    )

    def predict(self, features):
        return np.array([features.iloc[0]["humidity"] + features.iloc[0]["hour"]])


def build_prediction_csv(rows=10):
    lines = [
        "date_time,is_holiday,air_pollution_index,humidity,wind_speed,"
        "wind_direction,visibility_in_miles,dew_point,temperature,rain_p_h,"
        "snow_p_h,clouds_all,weather_type,weather_description,traffic_volume"
    ]
    for hour in range(rows):
        humidity = 40 + hour
        weather_type = "Rain" if hour >= 8 else "Clear"
        description = "light rain" if weather_type == "Rain" else "clear sky"
        lines.append(
            "2026-06-01 "
            f"{hour:02d}:00:00,,100,{humidity},4,180,10,11,300,0,0,40,"
            f"{weather_type},{description},1000"
        )
    return "\n".join(lines)


def fake_s3_client(csv_content):
    return type(
        "FakeS3Client",
        (),
        {"get_object": lambda self, Bucket, Key: {"Body": BytesIO(csv_content.encode("utf-8"))}},
    )()


class PredictionServiceTests(unittest.TestCase):
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

    def seed_region(self):
        user = User(
            email="user@example.com",
            password_hash="hash",
            role=UserRole.USER,
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
        self.session.flush()
        training_run = TrainingRun(
            region_id=region.id,
            dataset_id=dataset.id,
            status=TrainingRunStatus.COMPLETED,
            split_policy="time_series_cv_70_15_15",
            configuration_json={
                "train_start_date": "2026-06-01",
                "train_end_date": "2026-06-01 08:00:00",
            },
            requested_by=user.id,
        )
        self.session.add(training_run)
        self.session.commit()
        return user, region, training_run

    def test_predict_for_region_requires_active_model(self):
        user, region, _training_run = self.seed_region()
        payload = PredictionCreate(
            forecast_for=datetime(2026, 6, 19, 8, tzinfo=UTC),
        )

        with self.assertRaises(ApplicationError) as context:
            predict_for_region(self.session, region.id, payload, user)

        self.assertEqual(context.exception.code, "active_model_not_configured")

    def test_predict_for_region_persists_prediction(self):
        user, region, training_run = self.seed_region()
        with tempfile.TemporaryDirectory() as directory:
            model_path = os.path.join(directory, "model.pkl")
            joblib.dump(DummyPredictionModel(), model_path)
            model_version = ModelVersion(
                region_id=region.id,
                training_run_id=training_run.id,
                variant="Dummy",
                version="model_v1",
                mlflow_run_id="run-1",
                mlflow_model_uri="runs:/run-1/model",
                artifact_uri=model_path,
                cv_mean_mae=1.0,
                cv_std_mae=0.1,
                final_test_mae=1.2,
                status=ModelVersionStatus.ACTIVE,
            )
            self.session.add(model_version)
            self.session.flush()
            region.active_model_version_id = model_version.id
            self.session.commit()

            payload = PredictionCreate(
                forecast_for=datetime(2026, 6, 1, 8, tzinfo=UTC),
            )
            with patch(
                "backend.app.services.predictions.get_s3_client",
                return_value=fake_s3_client(build_prediction_csv()),
            ):
                prediction, used_model = predict_for_region(
                    self.session,
                    region.id,
                    payload,
                    user,
                )

        stored_predictions = self.session.query(Prediction).all()
        self.assertEqual(used_model.id, model_version.id)
        self.assertEqual(prediction.prediction, 56)
        self.assertEqual(len(stored_predictions), 1)
        self.assertEqual(stored_predictions[0].user_id, user.id)
        self.assertIn("model_features", stored_predictions[0].feature_snapshot_json)
        self.assertIn("production_dataset", stored_predictions[0].feature_snapshot_json["feature_source"])

    def test_predict_for_region_rejects_time_outside_production_window(self):
        user, region, training_run = self.seed_region()
        with tempfile.TemporaryDirectory() as directory:
            model_path = os.path.join(directory, "model.pkl")
            joblib.dump(DummyPredictionModel(), model_path)
            model_version = ModelVersion(
                region_id=region.id,
                training_run_id=training_run.id,
                variant="Dummy",
                version="model_v1",
                mlflow_run_id="run-1",
                mlflow_model_uri="runs:/run-1/model",
                artifact_uri=model_path,
                cv_mean_mae=1.0,
                cv_std_mae=0.1,
                final_test_mae=1.2,
                status=ModelVersionStatus.ACTIVE,
            )
            self.session.add(model_version)
            self.session.flush()
            region.active_model_version_id = model_version.id
            self.session.commit()

            payload = PredictionCreate(
                forecast_for=datetime(2026, 6, 1, 7, tzinfo=UTC),
            )

            with patch(
                "backend.app.services.predictions.get_s3_client",
                return_value=fake_s3_client(build_prediction_csv()),
            ):
                with self.assertRaises(ApplicationError) as context:
                    predict_for_region(self.session, region.id, payload, user)

        self.assertEqual(context.exception.code, "prediction_outside_production_window")

    def test_prediction_window_returns_active_model_production_bounds(self):
        user, region, training_run = self.seed_region()
        with tempfile.TemporaryDirectory() as directory:
            model_path = os.path.join(directory, "model.pkl")
            joblib.dump(DummyPredictionModel(), model_path)
            model_version = ModelVersion(
                region_id=region.id,
                training_run_id=training_run.id,
                variant="Dummy",
                version="model_v1",
                mlflow_run_id="run-1",
                mlflow_model_uri="runs:/run-1/model",
                artifact_uri=model_path,
                cv_mean_mae=1.0,
                cv_std_mae=0.1,
                final_test_mae=1.2,
                status=ModelVersionStatus.ACTIVE,
            )
            self.session.add(model_version)
            self.session.flush()
            region.active_model_version_id = model_version.id
            self.session.commit()

            with patch(
                "backend.app.services.predictions.get_s3_client",
                return_value=fake_s3_client(build_prediction_csv()),
            ):
                window = get_prediction_window(self.session, region.id)

        self.assertEqual(window["available_points"], 2)
        self.assertEqual(window["production_start_at"].hour, 8)
        self.assertEqual(window["production_end_at"].hour, 9)


if __name__ == "__main__":
    unittest.main()
