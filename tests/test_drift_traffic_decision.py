"""Retrain decision: traffic PSI drift and scheduled retrain (experiment E3). Dispatch is mocked."""
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.config import Settings
from backend.app.db.models import (
    Base, DriftCheckStatus, Region, TrainingRun, TrainingRunStatus, User, UserRole,
)
from backend.app.services import drift_monitoring as drift, drift_settings
from src.feature_drift import calculate_traffic_drift


def hourly(start, days, level, seed):
    rng = np.random.default_rng(seed)
    times = pd.date_range(start, periods=days * 24, freq="h")
    profile = 1 + 0.8 * np.sin(np.arange(len(times)) * 2 * np.pi / 24)
    return pd.DataFrame({"date_time": times, "traffic_volume": level * profile + rng.normal(0, 30, len(times)),
                         "temperature": rng.normal(280, 5, len(times))})


class TrafficDriftMetricTests(unittest.TestCase):
    def test_same_traffic_is_not_drift(self):
        result = calculate_traffic_drift(hourly("2026-01-01", 30, 1000, 1), hourly("2026-02-01", 7, 1000, 2))
        self.assertLess(result["value"], 1.0)
        self.assertFalse(result["drift"])
        self.assertEqual((result["metric"], result["column"], result["threshold"]), ("psi", "traffic_volume", 1.0))

    def test_thirty_percent_drop_is_drift(self):
        result = calculate_traffic_drift(hourly("2026-01-01", 30, 1000, 1), hourly("2026-02-01", 7, 700, 2))
        self.assertGreater(result["value"], 1.0)
        self.assertTrue(result["drift"])

    def test_missing_traffic_column_never_triggers(self):
        reference = hourly("2026-01-01", 30, 1000, 1).drop(columns="traffic_volume")
        result = calculate_traffic_drift(reference, hourly("2026-02-01", 7, 700, 2))
        self.assertIsNone(result["value"])
        self.assertFalse(result["drift"])


class RetrainDecisionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.admin = User(email="admin@example.test", password_hash="unused", role=UserRole.ADMIN)
        self.region = Region(name="A", latitude=1, longitude=1, timezone="UTC")
        self.db.add_all([self.admin, self.region])
        self.db.commit()
        self.model_id = uuid.uuid4()
        self.enable(True)

    def enable(self, enabled):
        drift_settings.update_settings(self.db, self.region.id, enabled, self.admin.id)

    def add_run(self, *, days_ago, source_model=None, status=TrainingRunStatus.COMPLETED, trigger="feature_drift"):
        # COMPLETED: a finished retrain whose candidate waits for Activate (a QUEUED run would hit the running guard).
        self.db.add(TrainingRun(region_id=self.region.id, dataset_id=uuid.uuid4(), status=status, split_policy="time",
                                requested_by=self.admin.id, created_at=datetime.now(UTC) - timedelta(days=days_ago),
                                configuration_json={"trigger_source": trigger,
                                                    "retrain_source_model_id": str(source_model or uuid.uuid4())}))
        self.db.commit()

    def check(self, *, weather=False, traffic=False, train_end="2026-01-03", current_end="2026-01-05",
              scheduled_days=28, method="auto", forced=False):
        frame = pd.DataFrame({"date_time": pd.date_range("2025-11-01", "2026-12-31", freq="D"), "traffic_volume": 1.0})
        source = SimpleNamespace(configuration_json={"train_end_date": train_end})
        model = SimpleNamespace(id=self.model_id)
        settings = Settings(drift_min_window_rows=1, drift_scheduled_retrain_days=scheduled_days)
        with ExitStack() as stack:
            stack.enter_context(patch.object(drift, "get_settings", return_value=settings))
            stack.enter_context(patch.object(drift, "_active_context", return_value=(model, source, SimpleNamespace(id=uuid.uuid4()))))
            stack.enter_context(patch.object(drift, "_download_dataset_frame", return_value=frame))
            stack.enter_context(patch.object(drift, "_production_frame",
                                             return_value=frame[frame.date_time >= pd.Timestamp(train_end)]))
            stack.enter_context(patch.object(drift, "calculate_feature_drift", return_value={
                "summary": {"drift_detected": weather, "drifted_feature_count": 6 if weather else 0, "feature_count": 13},
                "features": {}}))
            stack.enter_context(patch.object(drift, "calculate_traffic_drift", return_value={
                "metric": "psi", "column": "traffic_volume", "value": 2.5 if traffic else 0.2,
                "threshold": 1.0, "drift": traffic}))
            trigger = stack.enter_context(patch.object(drift, "_trigger_retraining", return_value=SimpleNamespace(id=uuid.uuid4())))
            stack.enter_context(patch.object(drift, "_create_check", side_effect=lambda db, **kwargs: kwargs))
            result = drift._check_region(self.db, self.region, auto_retrain=True, force_retrain=forced,
                                         check_method=method, current_end_at=current_end)
        self.assertNotEqual(result["status"], DriftCheckStatus.FAILED, result.get("error_message"))
        return result, trigger

    def test_weather_drift_alone_no_longer_retrains_or_alerts(self):
        result, trigger = self.check(weather=True)
        summary = result["drift_report"]["summary"]
        self.assertEqual(result["status"], DriftCheckStatus.STABLE)
        self.assertFalse(summary["drift_detected"])
        self.assertTrue(summary["weather_drift_detected"])
        self.assertEqual(summary["decision_signal"], "traffic_volume_psi")
        trigger.assert_not_called()

    def test_traffic_drift_triggers_retrain_with_reason(self):
        result, trigger = self.check(traffic=True)
        report = result["drift_report"]
        self.assertEqual(result["status"], DriftCheckStatus.RETRAIN_TRIGGERED)
        self.assertEqual((report["summary"]["traffic_psi"], report["summary"]["traffic_psi_threshold"]), (2.5, 1.0))
        self.assertEqual(report["retrain_reason"], "traffic_drift")
        self.assertEqual(trigger.call_args.kwargs["reason"], "traffic_drift")

    def test_old_model_gets_scheduled_retrain_without_drift(self):
        result, trigger = self.check(train_end="2026-01-03", current_end="2026-02-01")   # 29 days
        self.assertEqual(result["status"], DriftCheckStatus.RETRAIN_TRIGGERED)
        self.assertEqual(result["drift_report"]["retrain_reason"], "scheduled")
        self.assertEqual(trigger.call_args.kwargs["reason"], "scheduled")
        self.assertEqual(result["drift_report"]["summary"]["model_data_age_days"], 29)
        self.assertFalse(result["drift_report"]["summary"]["drift_detected"])

    def test_young_model_has_no_scheduled_retrain(self):
        result, trigger = self.check(train_end="2026-01-03", current_end="2026-01-30")   # 27 days
        self.assertEqual(result["status"], DriftCheckStatus.STABLE)
        trigger.assert_not_called()

    def test_recent_drift_retrain_postpones_the_schedule(self):
        self.add_run(days_ago=10)
        result, trigger = self.check(current_end="2026-03-01")
        self.assertEqual(result["status"], DriftCheckStatus.STABLE)
        trigger.assert_not_called()

    def test_failed_or_manual_runs_do_not_postpone_the_schedule(self):
        self.add_run(days_ago=5, status=TrainingRunStatus.FAILED)
        self.add_run(days_ago=5, status=TrainingRunStatus.CANCELLED)
        self.add_run(days_ago=5, trigger=None)
        result, trigger = self.check(current_end="2026-03-01")
        self.assertEqual(result["drift_report"]["retrain_reason"], "scheduled")
        trigger.assert_called_once()

    def test_old_drift_retrain_allows_the_next_schedule(self):
        self.add_run(days_ago=30)
        result, _ = self.check(current_end="2026-03-01")
        self.assertEqual(result["drift_report"]["retrain_reason"], "scheduled")

    def test_pending_candidate_from_same_model_is_not_retrained_again(self):
        self.add_run(days_ago=4, source_model=self.model_id)
        result, trigger = self.check(traffic=True)
        self.assertEqual(result["status"], DriftCheckStatus.DRIFT_DETECTED)
        self.assertEqual(result["drift_report"]["retrain_skip_reason"], "candidate_pending")
        trigger.assert_not_called()

    def test_traffic_drift_after_a_new_model_is_activated_retrains(self):
        self.add_run(days_ago=4, source_model=uuid.uuid4())   # retrain came from the previous active model
        result, trigger = self.check(traffic=True)
        self.assertEqual(result["drift_report"]["retrain_reason"], "traffic_drift")
        trigger.assert_called_once()

    def test_admin_force_on_traffic_drift_ignores_pending_candidate(self):
        self.add_run(days_ago=4, source_model=self.model_id)
        result, trigger = self.check(traffic=True, method="manual", forced=True)
        self.assertEqual(result["status"], DriftCheckStatus.RETRAIN_TRIGGERED)
        trigger.assert_called_once()

    def test_schedule_can_be_disabled(self):
        result, trigger = self.check(current_end="2026-03-01", scheduled_days=0)
        self.assertEqual(result["status"], DriftCheckStatus.STABLE)
        trigger.assert_not_called()

    def test_region_switch_off_stops_both_reasons(self):
        self.enable(False)
        for kwargs in ({"traffic": True}, {"current_end": "2026-03-01"}):
            with self.subTest(**kwargs):
                result, trigger = self.check(**kwargs)
                self.assertNotEqual(result["status"], DriftCheckStatus.RETRAIN_TRIGGERED)
                trigger.assert_not_called()

    def test_trigger_records_reason_in_training_configuration(self):
        settings = Settings(training_control_enabled=True, cloud_training_enabled=True)
        training_run = SimpleNamespace(id=uuid.uuid4(), configuration_json={"train_end_date": "2026-01-03"})
        dataset = SimpleNamespace(id=uuid.uuid4(), region_id=self.region.id, uploaded_by=self.admin.id)
        captured = {}

        def queued(db, dataset_id, user_id, configuration, dag_run_id):
            captured.update(configuration)
            return SimpleNamespace(id=uuid.uuid4())

        with ExitStack() as stack:
            stack.enter_context(patch.object(drift, "get_settings", return_value=settings))
            stack.enter_context(patch.object(drift, "_active_context",
                                             return_value=(SimpleNamespace(id=self.model_id), training_run, dataset)))
            stack.enter_context(patch.object(drift, "_download_dataset_frame", return_value=pd.DataFrame()))
            stack.enter_context(patch("backend.app.services.retrain_selection.prepare_retrain_selection", return_value={}))
            stack.enter_context(patch.object(drift.model_registry, "create_queued_training_run", side_effect=queued))
            stack.enter_context(patch.object(drift.airflow_client, "trigger_dag"))
            drift._trigger_retraining(self.db, dataset, training_run, pd.Timestamp("2026-02-01"), reason="scheduled")
        self.assertEqual(captured["trigger_source"], "feature_drift")
        self.assertEqual(captured["trigger_reason"], "scheduled")
        self.assertEqual(captured["retrain_source_model_id"], str(self.model_id))


if __name__ == "__main__":
    unittest.main()
