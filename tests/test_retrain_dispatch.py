"""Dispatch is mocked: no real requests, training or database changes."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

import pandas as pd

from backend.app.api.routes import datasets
from backend.app.core.errors import ApplicationError
from backend.app.schemas.model_version import TrainingRunCreate
from backend.app.services import drift_monitoring as drift


class RetrainDispatchTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.region = SimpleNamespace(id=uuid.uuid4())
        self.dataset = SimpleNamespace(id=uuid.uuid4(), region_id=self.region.id, uploaded_by=uuid.uuid4())
        self.model = SimpleNamespace(id=uuid.uuid4())
        self.source = SimpleNamespace(id=uuid.uuid4(), configuration_json={"training_backend": "kaggle",
                    "selected_models": ["random_forest_lag", "lstm", "gru"], "shap": {}})
        self.queued = SimpleNamespace(id=uuid.uuid4())
        self.settings = SimpleNamespace(training_control_enabled=True, cloud_training_enabled=True,
                                        training_dag_id="probe", drift_retrain_window_months=24)

    def test_manual_retrain_builds_trusted_configuration_but_first_train_does_not_select(self):
        self.db.get.return_value = self.region
        for retrain in (False, True):
            payload = TrainingRunCreate(train_start_date="2025-01-01", train_end_date="2026-01-01",
                       training_backend="kaggle", trigger_source="feature_drift" if retrain else None,
                       retrain_source_model_id=self.model.id if retrain else None)
            with self.subTest(retrain=retrain), patch.object(datasets, "get_settings", return_value=self.settings), \
                    patch.object(datasets.dataset_service, "get_dataset_or_404", return_value=self.dataset), \
                    patch.object(drift, "_active_context", return_value=(self.model, self.source, self.dataset)), \
                    patch.object(drift, "_download_dataset_frame", return_value=pd.DataFrame()), \
                    patch("backend.app.services.retrain_selection.prepare_retrain_selection", return_value={"enabled": True}) as selection, \
                    patch.object(datasets.model_registry, "create_queued_training_run", return_value=self.queued) as create, \
                    patch.object(datasets.airflow_client, "trigger_dag", return_value={}) as trigger, \
                    patch.object(datasets, "TrainingRunTriggerResponse", side_effect=lambda **kw: kw):
                datasets.trigger_dataset_training(self.dataset.id, payload, self.db, SimpleNamespace(id=uuid.uuid4()))
                config = create.call_args.args[3]
                self.assertEqual("feature_selection" in config, retrain)
                self.assertEqual(selection.call_count, int(retrain))
                self.assertEqual(config["_execution_protocol"], 2)
                if retrain:
                    self.assertEqual(config["retrain_source_model_id"], str(self.model.id))
                self.assertEqual(trigger.call_count, 1)

    def test_stale_manual_source_is_rejected_before_queueing(self):
        self.db.get.return_value = self.region
        payload = TrainingRunCreate(train_start_date="2025-01-01", train_end_date="2026-01-01",
                    trigger_source="feature_drift", retrain_source_model_id=uuid.uuid4())
        with patch.object(datasets, "get_settings", return_value=self.settings), \
                patch.object(datasets.dataset_service, "get_dataset_or_404", return_value=self.dataset), \
                patch.object(drift, "_active_context", return_value=(self.model, self.source, self.dataset)), \
                patch.object(datasets.model_registry, "create_queued_training_run") as create:
            with self.assertRaises(ApplicationError) as caught:
                datasets.trigger_dataset_training(self.dataset.id, payload, self.db, SimpleNamespace(id=uuid.uuid4()))
            self.assertEqual(caught.exception.code, "retrain_source_changed")
            create.assert_not_called()

    def test_auto_inherits_cloud_and_creates_controlled_execution(self):
        with patch.object(drift, "get_settings", return_value=self.settings), \
                patch.object(drift, "_active_context", return_value=(self.model, self.source, self.dataset)), \
                patch.object(drift, "_download_dataset_frame", return_value=pd.DataFrame()), \
                patch("backend.app.services.retrain_selection.prepare_retrain_selection", return_value={"enabled": True}), \
                patch.object(drift.model_registry, "create_queued_training_run", return_value=self.queued) as create, \
                patch.object(drift.airflow_client, "trigger_dag") as trigger:
            drift._trigger_retraining(self.db, self.dataset, self.source, pd.Timestamp("2026-02-01"))
            config = create.call_args.args[3]
            self.assertEqual(config["training_backend"], "kaggle")
            self.assertEqual(config["_execution_protocol"], 2)
            self.assertTrue(config["feature_selection"]["enabled"])
            self.assertEqual(trigger.call_args.args[1]["training_run_id"], str(self.queued.id))

    def test_auto_cloud_unavailable_never_falls_back_or_queues(self):
        self.settings.cloud_training_enabled = False
        with patch.object(drift, "get_settings", return_value=self.settings), \
                patch.object(drift.model_registry, "create_queued_training_run") as create:
            with self.assertRaisesRegex(ValueError, "fallback"):
                drift._trigger_retraining(self.db, self.dataset, self.source, pd.Timestamp("2026-02-01"))
            create.assert_not_called()

    def test_auto_dispatch_error_closes_controlled_state(self):
        with patch.object(drift, "get_settings", return_value=self.settings), \
                patch.object(drift, "_active_context", return_value=(self.model, self.source, self.dataset)), \
                patch.object(drift, "_download_dataset_frame", return_value=pd.DataFrame()), \
                patch("backend.app.services.retrain_selection.prepare_retrain_selection", return_value={"enabled": True}), \
                patch.object(drift.model_registry, "create_queued_training_run", return_value=self.queued), \
                patch.object(drift.airflow_client, "trigger_dag", side_effect=RuntimeError("synthetic timeout")), \
                patch("backend.app.services.training_execution.fail_dispatch") as fail:
            with self.assertRaises(RuntimeError):
                drift._trigger_retraining(self.db, self.dataset, self.source, pd.Timestamp("2026-02-01"))
            fail.assert_called_once_with(self.db, self.queued.id)


if __name__ == "__main__":
    unittest.main()
