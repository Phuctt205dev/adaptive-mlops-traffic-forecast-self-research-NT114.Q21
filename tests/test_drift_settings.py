"""Region opt-in and API persistence; dispatch is mocked, never real training."""
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.api.dependencies import get_current_user
from backend.app.api.routes.drift import router
from backend.app.core.config import Settings
from backend.app.core.errors import ApplicationError, register_exception_handlers
from backend.app.db.models import Base, AuditLog, Region, User, UserRole, DriftCheckStatus
from backend.app.db.session import get_db
from backend.app.services import drift_monitoring as drift, drift_settings as settings


class DriftSettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.admin = User(email="admin@example.test", password_hash="unused", role=UserRole.ADMIN)
        self.regions = [Region(name=name, latitude=1, longitude=1, timezone="UTC") for name in ("A", "B")]
        self.db.add_all([self.admin, *self.regions])
        self.db.commit()
        self.a, self.b = [r.id for r in self.regions]
        self.app = FastAPI()
        register_exception_handlers(self.app)
        self.app.include_router(router)
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[get_current_user] = lambda: self.admin
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def path(self, region_id):
        return f"/regions/{region_id}/drift-settings"

    def save(self, region_id, enabled):
        response = self.client.patch(self.path(region_id), json={"auto_retrain": enabled})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_defaults_off_for_each_region_without_writing(self):
        for rid in (self.a, self.b):
            self.assertEqual(self.client.get(self.path(rid)).json(), {"region_id": str(rid), "auto_retrain": False})
        self.assertEqual(self.db.scalar(select(func.count()).select_from(AuditLog)), 0)

    def test_persists_across_sessions_and_isolates_regions(self):
        self.save(self.a, True)
        with Session(self.engine) as replica:
            self.assertTrue(settings.get_settings(replica, self.a)["auto_retrain"])
            self.assertFalse(settings.get_settings(replica, self.b)["auto_retrain"])
        self.save(self.b, True)
        self.save(self.a, False)
        self.assertFalse(self.client.get(self.path(self.a)).json()["auto_retrain"])
        self.assertTrue(self.client.get(self.path(self.b)).json()["auto_retrain"])
        entries = list(self.db.scalars(select(AuditLog).where(AuditLog.action == settings.ACTION)))
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(e.actor_user_id == self.admin.id for e in entries))

    def test_repeated_save_is_idempotent(self):
        self.save(self.a, True)
        self.save(self.a, True)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(AuditLog)), 1)

    def test_latest_change_wins_even_if_replica_clock_moves_backwards(self):
        with patch.object(settings, "datetime") as clock:
            clock.now.side_effect = [datetime(2026, 11, 16, 12, tzinfo=UTC), datetime(2026, 11, 16, 11, tzinfo=UTC)]
            self.save(self.a, True)
            self.save(self.a, False)
        self.assertFalse(settings.auto_retrain_enabled(self.db, self.a))

    def test_invalid_payload_and_missing_region_are_rejected(self):
        for value in (None, "false", 1, [], {}):
            with self.subTest(value=value):
                self.assertEqual(self.client.patch(self.path(self.a), json={"auto_retrain": value}).status_code, 422)
        self.assertEqual(self.client.patch(self.path(self.a), json={"auto_retrain": True, "region_id": str(self.b)}).status_code, 422)
        self.assertEqual(self.client.get(self.path(uuid.uuid4())).status_code, 404)
        self.assertEqual(self.client.patch(self.path(uuid.uuid4()), json={"auto_retrain": True}).status_code, 404)

    def test_only_admin_can_read_or_change_settings(self):
        self.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role=UserRole.USER)
        self.assertEqual(self.client.get(self.path(self.a)).status_code, 403)
        self.assertEqual(self.client.patch(self.path(self.a), json={"auto_retrain": True}).status_code, 403)

    def check(self, rid, *, enabled=True, forced=False, method="auto", global_enabled=True, running=False, cooldown=False, detected=True):
        frame = pd.DataFrame({"date_time": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-04", "2026-01-05"])})
        source = SimpleNamespace(configuration_json={"train_end_date": "2026-01-03"})
        dataset = SimpleNamespace(id=uuid.uuid4())
        model = SimpleNamespace(id=uuid.uuid4())
        with ExitStack() as stack:
            stack.enter_context(patch.object(drift, "get_settings", return_value=Settings(drift_min_window_rows=1, drift_auto_retrain=global_enabled)))
            stack.enter_context(patch.object(drift, "_active_context", return_value=(model, source, dataset)))
            stack.enter_context(patch.object(drift, "_download_dataset_frame", return_value=frame))
            stack.enter_context(patch.object(drift, "_production_frame", return_value=frame.iloc[2:]))
            stack.enter_context(patch.object(drift, "calculate_feature_drift", return_value={"summary": {"drift_detected": detected}}))
            stack.enter_context(patch.object(drift, "_has_running_training", return_value=running))
            stack.enter_context(patch.object(drift, "_has_recent_retrain", return_value=cooldown))
            trigger = stack.enter_context(patch.object(drift, "_trigger_retraining", return_value=SimpleNamespace(id=uuid.uuid4())))
            stack.enter_context(patch.object(drift, "_create_check", side_effect=lambda db, **kwargs: kwargs))
            result = drift._check_region(self.db, self.db.get(Region, rid), auto_retrain=enabled,
                force_retrain=forced, check_method=method, current_end_at="2026-01-05")
            self.assertNotEqual(result["status"], DriftCheckStatus.FAILED, result.get("error_message"))
            return result, trigger.call_count

    def test_scheduled_and_manual_checks_respect_each_regions_saved_choice(self):
        self.save(self.a, True)
        for method in ("auto", "manual"):
            for rid, expected in ((self.a, 1), (self.b, 0)):
                with self.subTest(method=method, rid=rid):
                    _, triggered = self.check(rid, method=method)
                    self.assertEqual(triggered, expected)
        self.save(self.a, False)
        self.assertEqual(self.check(self.a)[1], 0)

    def test_global_and_request_switches_still_suppress_retraining(self):
        self.save(self.a, True)
        for kwargs in ({"enabled": False}, {"global_enabled": False}, {"detected": False}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.check(self.a, **kwargs)[1], 0)

    def test_internal_force_cannot_opt_unselected_region_in(self):
        self.assertEqual(self.check(self.b, forced=True, method="auto")[1], 0)
        self.assertEqual(self.check(self.b, forced=True, method="manual")[1], 1)

    def test_running_training_and_cooldown_guards_remain(self):
        self.save(self.a, True)
        for kwargs in ({"running": True}, {"cooldown": True}):
            self.assertEqual(self.check(self.a, **kwargs)[1], 0)


if __name__ == "__main__":
    unittest.main()
