import unittest
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.api.routes.system import _count_by_status
from backend.app.core.errors import ApplicationError
from backend.app.core.middleware import register_operational_middleware
from backend.app.core.rate_limit import check_rate_limit
from backend.app.db.models import Base, Dataset, DatasetStatus, Region, User, UserRole


class Phase10OperationsTests(unittest.TestCase):
    def test_rate_limit_rejects_after_limit(self):
        key = f"test-phase10-rate-limit:{uuid.uuid4()}"
        check_rate_limit(key, limit=2, window_seconds=60)
        check_rate_limit(key, limit=2, window_seconds=60)

        with self.assertRaises(ApplicationError) as context:
            check_rate_limit(key, limit=2, window_seconds=60)

        self.assertEqual(context.exception.code, "rate_limit_exceeded")
        self.assertEqual(context.exception.status_code, 429)

    def test_security_headers_are_added(self):
        app = FastAPI()
        register_operational_middleware(app)

        @app.get("/ping")
        def ping():
            return {"status": "ok"}

        response = TestClient(app).get("/ping")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("camera=()", response.headers["Permissions-Policy"])

    def test_count_by_status(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        try:
            with Session(engine) as session:
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
                session.add_all([user, region])
                session.flush()
                session.add_all(
                    [
                        Dataset(
                            region_id=region.id,
                            contract_version="traffic-training-csv/v1",
                            original_filename="a.csv",
                            storage_uri="s3://bucket/a.csv",
                            sha256="a" * 64,
                            status=DatasetStatus.VALID,
                            uploaded_by=user.id,
                        ),
                        Dataset(
                            region_id=region.id,
                            contract_version="traffic-training-csv/v1",
                            original_filename="b.csv",
                            storage_uri="s3://bucket/b.csv",
                            sha256="b" * 64,
                            status=DatasetStatus.VALIDATION_FAILED,
                            uploaded_by=user.id,
                        ),
                    ]
                )
                session.commit()

                counts = _count_by_status(session, Dataset, Dataset.status)
        finally:
            Base.metadata.drop_all(engine)

        self.assertEqual(counts["valid"], 1)
        self.assertEqual(counts["validation_failed"], 1)


if __name__ == "__main__":
    unittest.main()
