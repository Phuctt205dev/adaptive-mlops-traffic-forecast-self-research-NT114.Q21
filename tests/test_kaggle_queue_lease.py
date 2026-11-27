"""A notebook waiting in Kaggle's queue is never Quick Saved; its first lease covers the wait.

Run 98f8a8bd (27/11, LSTM on GPU): Kaggle queued version 1 within 5 s while the
same account had just started two GPU sessions at once by hand. The worker then
Quick Saved the notebook every ~45 s to renew its 180 s lease; each save showed on
Kaggle as a new Failed version and version 1 never left the queue. Now the push
writes a 15-minute start lease, nothing is written while the run is queued, and a
lease that has expired proves the notebook can no longer train, so the branch ends
by itself (queue timeout, or Stop) without anyone cancelling it on Kaggle.
"""
import copy
from pathlib import Path
import re
import tempfile
import unittest
import uuid

from src.cloud_training.control import CONTROL_PREFIX, LEASE_SECONDS, START_LEASE_SECONDS
from src.cloud_training.kaggle_execution import KaggleExecution, LEASE_EXPIRY_MARGIN, TrainingCancelled, initial_ledger
from src.cloud_training.kaggle_transport import ProviderError
from tests.test_kaggle_execution import FakeTransport
import json


def lease_of(source):
    line = [item for item in source.splitlines() if item.startswith(CONTROL_PREFIX)][0]
    return json.loads(line[len(CONTROL_PREFIX):])


class QueueLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "input").mkdir()
        context = {**{key: str(uuid.uuid4()) for key in ("run_id", "region_id", "dataset_id", "attempt_id")},
                   "branch": "lstm", "configuration_sha256": "a" * 64}
        self.ledger = initial_ledger({"context": context, "files": {}}, "probeuser", "gpu")
        self.now = [1_000_000.0]
        self.saved, self.transport = [], FakeTransport()
        self.provider = self.make(self.ledger)

    def make(self, ledger):
        return KaggleExecution(self.transport, self.root, ledger, lambda value: self.saved.append(copy.deepcopy(value)),
                               clock=lambda: self.now[0], sleep=lambda seconds: self.now.__setitem__(0, self.now[0] + seconds),
                               background_progress=False)

    def quick_saves(self):
        return [values for op, values in self.transport.calls if op == "kernel_push" and values["no_run"]]

    def test_push_writes_a_start_lease_that_covers_the_queue_and_journals_it_first(self):
        pushed_at = self.now[0]
        self.provider.submit()
        control = lease_of(self.transport.source)
        self.assertEqual(control["lease_until"], pushed_at + START_LEASE_SECONDS)
        self.assertFalse(control["cancel"])
        fenced = [item for item in self.saved if item.get("submit_pending")][0]
        self.assertEqual(fenced["lease_until"], pushed_at + START_LEASE_SECONDS)

    def test_queued_notebook_is_never_quick_saved_and_times_out_when_its_lease_ends(self):
        # A CPU notebook (a GPU one falls back to CPU after two minutes instead).
        self.ledger["accelerator"] = "cpu"
        self.provider = self.make(self.ledger)
        self.transport.state = "QUEUED"
        submitted = self.now[0]
        with self.assertRaises(ProviderError) as caught:
            self.provider.run(lambda event: None, lambda: False)
        self.assertEqual(caught.exception.category, "queue_timeout")
        self.assertEqual(self.quick_saves(), [])
        self.assertGreaterEqual(self.now[0], submitted + START_LEASE_SECONDS)
        self.assertLess(self.now[0], submitted + START_LEASE_SECONDS + 60)
        # The branch can be closed at once: the lease that the queued run would read has ended.
        self.now[0] += LEASE_EXPIRY_MARGIN
        self.assertTrue(self.provider.stop())
        self.assertEqual(self.quick_saves(), [])
        self.assertTrue(self.provider.cleanup())
        self.assertFalse(any(self.transport.resources.values()))

    def test_lease_is_renewed_in_time_after_the_notebook_starts_running(self):
        # Renewal is due RENEW_BEFORE_SECONDS before the newest lease ends, not at once:
        # a notebook that waited in the queue must still never run on an expired lease.
        self.ledger["accelerator"] = "cpu"
        self.provider = self.make(self.ledger)
        self.transport.state = "QUEUED"
        started = {}
        original = self.transport.call
        def call(operation, **values):
            if operation == "status" and self.now[0] >= 1_000_000.0 + 120 and self.transport.state == "QUEUED":
                self.transport.state = "RUNNING"
                started["at"] = self.now[0]
            return original(operation, **values)
        self.transport.call = call
        with self.assertRaises(TrainingCancelled):
            self.provider.run(lambda event: None, lambda: bool(self.quick_saves()))
        saves = self.quick_saves()
        self.assertEqual(len(saves), 1)
        self.assertIn("at", started)
        renewed = lease_of(self.transport.source)
        # Renewed before the start lease ended, with a long lease again.
        self.assertLess(self.now[0], 1_000_000.0 + START_LEASE_SECONDS)
        self.assertGreaterEqual(renewed["lease_until"], self.now[0] + START_LEASE_SECONDS - 10)

    def test_stop_of_a_queued_notebook_ends_once_the_cancel_lease_has_expired(self):
        self.provider.submit()
        self.transport.state = "QUEUED"
        self.assertFalse(self.provider.stop())  # writes the cancel signal once (lease of 180 s)
        self.assertEqual(len(self.quick_saves()), 1)
        self.now[0] += LEASE_SECONDS
        self.assertFalse(self.provider.stop())
        self.now[0] += LEASE_EXPIRY_MARGIN
        self.assertTrue(self.provider.stop())
        self.assertEqual(len(self.quick_saves()), 1)
        self.assertTrue(self.provider.ledger["remote_stopped"])
        self.assertTrue(self.provider.cleanup())
        self.assertFalse(any(self.transport.resources.values()))

    def test_an_unconfirmed_later_lease_keeps_the_branch_open(self):
        self.provider.submit()
        self.transport.state = "QUEUED"
        ledger = copy.deepcopy(self.provider.ledger)
        ledger.update(control_pending=True, pending_control={"base_version": 1, "sha256": "0" * 64, "cancel": False,
                                                             "lease_until": self.now[0] + 2000, "writes": 1})
        provider = self.make(ledger)
        self.now[0] += START_LEASE_SECONDS + LEASE_EXPIRY_MARGIN
        self.assertFalse(provider._lease_expired())

    def test_notebook_accepts_the_start_lease_but_not_an_unbounded_one(self):
        template = (Path(__file__).resolve().parents[1] / "src/cloud_training/notebook.py").read_text(encoding="utf-8")
        windows = {int(value) for value in re.findall(r'control\["lease_until"\] <= time\.time\(\) \+ (\d+)', template)}
        self.assertEqual(len(windows), 1)
        window = windows.pop()
        self.assertGreaterEqual(window, START_LEASE_SECONDS + 60)
        self.assertLessEqual(window, 1800)


if __name__ == "__main__":
    unittest.main()
