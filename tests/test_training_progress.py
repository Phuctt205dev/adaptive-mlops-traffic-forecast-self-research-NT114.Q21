"""Exercise real progress hooks without fitting or requiring TensorFlow."""
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.training_progress import reporting, report, set_fold, start_fit, epoch_callbacks
from src.cloud_training.telemetry import source_timing


class TrainingProgressTests(unittest.TestCase):
    def test_source_clock_and_fit_identity_survive_epoch_reset_and_early_stop(self):
        events = []
        tensorflow = SimpleNamespace(keras=SimpleNamespace(callbacks=SimpleNamespace(Callback=object)))
        with patch.dict(sys.modules, {"tensorflow": tensorflow}), reporting(events.append):
            set_fold(1, 3)
            start_fit("LSTM", "cv_full")
            first = epoch_callbacks("LSTM", 10)[0]
            first.on_epoch_begin(0)
            first.on_epoch_end(0, {"loss": 1.0})
            first.on_train_end()
            start_fit("LSTM", "cv_reduced")
            second = epoch_callbacks("LSTM", 10)[0]
            second.on_epoch_begin(0)
            set_fold()
            report("LSTM", "model_cv_completed")
            start_fit("LSTM", "final")
            epoch_callbacks("LSTM", 3)[0].on_epoch_begin(0)
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        fits = [event for event in events if event["stage"] == "fit_started"]
        self.assertEqual([event["fit_purpose"] for event in fits], ["cv_full", "cv_reduced", "final"])
        self.assertEqual(len({event["fit_id"] for event in fits}), 3)
        stopped = next(event for event in events if event["stage"] == "fit_completed")
        self.assertTrue(stopped["early_stopped"])
        self.assertEqual(stopped["epochs_completed"], 1)
        self.assertNotIn("fold", fits[-1])
        for event in events:
            self.assertEqual(source_timing(event)["source_clock"], "ml_child_monotonic_v1")

    def test_disabled_hooks_do_not_import_tensorflow_or_change_work(self):
        with patch.dict(sys.modules, {"tensorflow": None}):
            start_fit("LSTM", "cv_full")
            self.assertEqual(epoch_callbacks("LSTM", 10), [])
            report("LSTM", "model_started")

    def test_reporting_scopes_reset_sequence_and_do_not_leak_fold_or_fit(self):
        first, second = [], []
        with reporting(first.append):
            set_fold(2, 3)
            start_fit("GRU", "cv_full")
        with reporting(second.append):
            report("GRU", "model_started")
        self.assertEqual(second[0]["sequence"], 1)
        self.assertNotIn("fold", second[0])
        self.assertNotIn("fit_id", second[0])

    def test_legacy_timing_is_unknown_and_invalid_clock_data_is_rejected(self):
        self.assertEqual(source_timing({}), {})
        for value in ({"source_time": "2026-11-09T00:00:00Z"},
                      {"source_time": "2026-11-09T00:00:00", "source_clock": "ml_child_monotonic_v1", "source_elapsed_seconds": 1},
                      {"source_time": "2026-11-09T00:00:00Z", "source_clock": "ml_child_monotonic_v1", "source_elapsed_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                source_timing(value)
