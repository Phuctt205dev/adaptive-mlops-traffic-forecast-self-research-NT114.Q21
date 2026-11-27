"""A notebook push whose reply is lost must neither hang the run nor start a second fit.

Reproduces run d65a2049 (27/11): `kernel_push` failed with a connection error after
0.014 s, no notebook was ever created on Kaggle, and Stop then retried `kernel_get`
404 forever. A replacement push is safe only once the notebook is confirmed absent
AND the lease written into the lost push has expired: if that push did commit and
becomes visible late, its notebook reads an expired lease at start and exits.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from src.cloud_training.control import CONTROL_PREFIX, LEASE_SECONDS
from src.cloud_training.kaggle_execution import (KaggleExecution, ProviderUncertain, SUBMIT_ATTEMPTS,
                                                 SUBMIT_LEASE_MARGIN, TrainingCancelled, initial_ledger)
from src.cloud_training.kaggle_transport import ProviderError
from tests.test_kaggle_execution import FakeTransport


class LossyTransport(FakeTransport):
    """`kernel_push` can fail before reaching Kaggle, or after Kaggle accepted it."""

    def __init__(self):
        super().__init__()
        self.push_failures = []      # per push: None (ok), "before" (nothing created), "after" (created, reply lost)
        self.visible_after = 0       # resource_exists reads that still miss a created notebook
        self.pushed_sources = []

    def call(self, operation, **values):
        if operation == "kernel_push" and not values["no_run"]:
            failure = self.push_failures.pop(0) if self.push_failures else None
            if failure == "before":
                self.calls.append((operation, values))
                self.pushed_sources.append(None)
                raise ProviderError("connection failed", operation="kernel_push", category="connection_error")
            result = super().call(operation, **values)
            self.pushed_sources.append(self.source)
            if failure == "after":
                raise ProviderError("reply lost", operation="kernel_push", category="connection_error")
            return result
        if operation == "resource_exists" and values["kind"] == "notebook" and self.resources["notebook"] and self.visible_after:
            self.calls.append((operation, values))
            self.visible_after -= 1
            return {"exists": False}
        if operation == "kernel_get" and not self.resources["notebook"]:
            self.calls.append((operation, values))
            raise ProviderError("not found", 404, operation="kernel_get", category="not_found")
        return super().call(operation, **values)


def lease(source):
    line = [item for item in source.splitlines() if item.startswith(CONTROL_PREFIX)][0]
    return json.loads(line[len(CONTROL_PREFIX):])["lease_until"]


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "input").mkdir()
        context = {**{key: str(uuid.uuid4()) for key in ("run_id", "region_id", "dataset_id", "attempt_id")},
                   "branch": "lstm", "configuration_sha256": "a" * 64}
        self.ledger = initial_ledger({"context": context, "files": {}}, "probeuser", "cpu")
        self.now = [1000.0]
        self.saved, self.transport = [], LossyTransport()
        self.provider = self.make(self.ledger)

    def make(self, ledger):
        return KaggleExecution(self.transport, self.root, ledger, lambda value: self.saved.append(copy.deepcopy(value)),
                               clock=lambda: self.now[0], sleep=lambda seconds: self.now.__setitem__(0, self.now[0] + seconds),
                               background_progress=False)

    def pushes(self):
        return [values for op, values in self.transport.calls if op == "kernel_push" and not values["no_run"]]


class SubmitRecoveryTests(Base):
    def test_push_that_never_reached_kaggle_is_sent_again_only_after_its_lease_expired(self):
        self.transport.push_failures = ["before"]
        started = self.now[0]
        self.provider.submit()
        self.assertEqual(len(self.pushes()), 2)
        self.assertEqual(self.provider.ledger["version"], 1)
        self.assertFalse(self.provider.ledger["submit_pending"])
        self.assertEqual(self.provider.ledger["submit_attempts"], 2)
        # The second push happened after the first notebook's lease could no longer be honoured.
        first_lease = lease(self.transport.pushed_sources[-1]) - LEASE_SECONDS  # render clock of push 2
        self.assertGreaterEqual(first_lease, started + LEASE_SECONDS + SUBMIT_LEASE_MARGIN)
        # The replacement carries a fresh lease, so it can start normally.
        self.assertGreater(lease(self.transport.source), self.now[0])

    def test_lost_reply_of_a_created_notebook_is_read_back_never_pushed_again(self):
        self.transport.push_failures = ["after"]
        self.provider.submit()
        self.assertEqual(len(self.pushes()), 1)
        self.assertEqual(self.provider.ledger["version"], 1)

    def test_notebook_that_becomes_visible_late_is_not_pushed_again(self):
        self.transport.push_failures = ["after"]
        self.transport.visible_after = 3
        self.provider.submit()
        self.assertEqual(len(self.pushes()), 1)
        self.assertEqual(self.provider.ledger["version"], 1)

    def test_pushes_are_bounded_and_a_confirmed_absence_ends_without_needs_attention(self):
        self.transport.push_failures = ["before"] * SUBMIT_ATTEMPTS
        with self.assertRaises(ProviderError) as caught:
            self.provider.submit()
        self.assertNotIsInstance(caught.exception, ProviderUncertain)
        self.assertEqual(caught.exception.category, "submit_failed")
        self.assertEqual(len(self.pushes()), SUBMIT_ATTEMPTS)
        # Nothing exists on Kaggle any more, so Stop needs no notebook read.
        self.assertFalse(self.provider.ledger["submit_pending"])
        self.assertTrue(self.provider.stop())

    def test_stop_during_the_absence_wait_never_pushes_again(self):
        self.transport.push_failures = ["before"]
        with self.assertRaises(TrainingCancelled):
            self.provider.submit(lambda: len(self.pushes()) >= 1)
        self.assertEqual(len(self.pushes()), 1)

    def test_restart_with_a_pending_push_reconciles_instead_of_failing_on_404(self):
        self.transport.push_failures = ["before"]
        original = self.transport.call
        def crash(operation, **values):
            if operation == "resource_exists" and values["kind"] == "notebook":
                raise RuntimeError("worker restarted")
            return original(operation, **values)
        self.transport.call = crash
        with self.assertRaises(RuntimeError):
            self.provider.submit()
        self.transport.call = original
        recovered = self.make(self.saved[-1])
        recovered.submit()
        self.assertEqual(len(self.pushes()), 2)
        self.assertEqual(recovered.ledger["version"], 1)


class StopNeverSubmittedTests(Base):
    """Stop of a branch whose push outcome was never confirmed (the d65a2049 journal)."""

    def stuck(self, **extra):
        ledger = {**self.ledger, "dataset_pending": True, "dataset_upload_confirmed": True, "submit_pending": True,
                  "version": None, "phase": "needs_attention",
                  "last_failure": {"operation": "kernel_push", "category": "connection_error", "http_status": None}, **extra}
        self.transport.resources["dataset"] = True
        return self.make(ledger)

    def test_absent_notebook_is_stopped_after_its_lease_window_then_input_is_removed(self):
        provider = self.stuck()  # journal written before submit_attempted_at existed
        self.assertFalse(provider.stop())
        self.now[0] += LEASE_SECONDS
        self.assertFalse(provider.stop())
        self.now[0] += SUBMIT_LEASE_MARGIN
        self.assertTrue(provider.stop())
        self.assertTrue(provider.ledger["remote_stopped"])
        self.assertTrue(provider.ledger["notebook_cleaned"])
        self.assertTrue(provider.cleanup())
        self.assertFalse(self.transport.resources["dataset"])
        self.assertFalse(any(op == "kernel_get" for op, _ in self.transport.calls))

    def test_known_push_time_is_used_and_reads_are_rate_limited(self):
        provider = self.stuck(submit_attempted_at=self.now[0] - LEASE_SECONDS - SUBMIT_LEASE_MARGIN)
        self.assertFalse(provider.stop())          # first absence read
        for _ in range(5):                         # watcher sweeps every 2 s
            self.now[0] += 2
            self.assertFalse(provider.stop())
        reads = [op for op, values in self.transport.calls if op == "resource_exists"]
        self.assertEqual(len(reads), 1)
        self.now[0] += 15
        self.assertTrue(provider.stop())

    def test_notebook_that_does_exist_takes_the_normal_cancel_path(self):
        provider = self.stuck()
        self.transport.source = None
        self.transport.metadata = provider.metadata()
        from src.cloud_training.control import render_notebook
        self.transport.source = render_notebook(provider.ledger["snapshot"], provider.ledger["reference"],
                                                provider.ledger["dataset_reference"], clock=self.now[0])
        self.transport.version, self.transport.state = 1, "QUEUED"
        self.transport.resources["notebook"] = True
        self.assertFalse(provider.stop())
        self.assertEqual(provider.ledger["version"], 1)
        self.assertFalse(provider.ledger["remote_stopped"])
        self.assertTrue(provider.ledger["cancel_requested"])


if __name__ == "__main__":
    unittest.main()
