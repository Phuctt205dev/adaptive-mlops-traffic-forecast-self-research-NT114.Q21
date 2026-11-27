"""A GPU branch that Kaggle does not start within two minutes trains on CPU instead.

Run 095dc119 (27/11): the GRU notebook was queued for 5.7 minutes on an empty account
while the LSTM notebook pushed 24 s later got a GPU at once; Kaggle does not say when
a GPU will be ready. The admin chose: keep asking for a GPU, but after a short wait
stop the queued GPU notebook (its cancel signal makes it exit if Kaggle ever starts
it) and run the same branch, on the same uploaded input, in a CPU notebook.

Also covers the running-lease fix: a notebook that was queued is renewed in time
once it runs, with long leases, so a normal run makes no Quick Save at all.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from src.cloud_training.control import (CONTROL_PREFIX, START_LEASE_SECONDS, render_notebook, source_configuration,
                                        source_control)
from src.cloud_training.kaggle_execution import (GPU_QUEUE_FALLBACK_SECONDS, RENEW_BEFORE_SECONDS, KaggleExecution,
                                                 TrainingCancelled, initial_ledger)
from src.cloud_training.kaggle_transport import ProviderError
from src.cloud_training.package import effective_accelerator


class KaggleAccount:
    """Several private notebooks, each with its own versions and run state."""

    def __init__(self):
        self.calls, self.kernels, self.dataset = [], {}, False
        self.default_state = "RUNNING"

    def call(self, operation, **values):
        self.calls.append((operation, copy.deepcopy(values)))
        if operation == "dataset_create":
            self.dataset = True
            return {"created": True}
        if operation == "dataset_check":
            return {"private": True, "version": 1, "status": "ready"}
        if operation == "kernel_push":
            root = Path(values["directory"])
            metadata = json.loads((root / "kernel-metadata.json").read_text())
            kernel = self.kernels.setdefault(metadata["id"], {"version": 0, "state": self.default_state, "runs": []})
            kernel.update(source=(root / "train.py").read_text(encoding="utf-8"), metadata=metadata)
            kernel["version"] += 1
            if not values["no_run"]:
                kernel["runs"].append({"version": kernel["version"], "gpu": metadata["enable_gpu"]})
            return {"version": kernel["version"]}
        if operation == "kernel_get":
            kernel = self.kernels.get(values["reference"])
            if kernel is None:
                raise ProviderError("not found", 404, operation="kernel_get", category="not_found")
            metadata = kernel["metadata"]
            return {"source": kernel["source"], "ref": metadata["id"], "current_version_number": kernel["version"],
                    "is_private": True, "enable_gpu": metadata["enable_gpu"], "enable_internet": True,
                    "dataset_data_sources": metadata["dataset_sources"], "kernel_data_sources": [],
                    "competition_data_sources": [], "model_data_sources": []}
        if operation == "status":
            return {"state": self.kernels[values["reference"].rsplit("/", 1)[0]]["state"]}
        if operation == "resource_exists":
            if values["kind"] == "dataset":
                return {"exists": self.dataset}
            return {"exists": values["reference"] in self.kernels}
        if operation == "cleanup":
            if values.get("notebook"):
                self.kernels.pop(values["notebook"], None)
            if values.get("dataset"):
                self.dataset = False
            return {"cleaned": True}
        raise AssertionError(operation)

    def stream(self, reference, context):
        class Silent:
            reader_failed = False
            def drain(self):
                return []
            def returncode(self):
                return None
            def stop(self):
                return True
        return Silent()

    def quick_saves(self, reference=None):
        return [values for op, values in self.calls if op == "kernel_push" and values["no_run"]
                and (reference is None or json.loads((Path(values["directory"]) / "kernel-metadata.json")
                                                     .read_text())["id"] == reference)]


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "input").mkdir()
        context = {**{key: str(uuid.uuid4()) for key in ("run_id", "region_id", "dataset_id", "attempt_id")},
                   "branch": "gru", "configuration_sha256": "a" * 64}
        self.now = [1_000_000.0]
        self.account = KaggleAccount()
        self.saved = []
        self.ledger = initial_ledger({"context": context, "files": {}}, "probeuser", "gpu")
        self.gpu_reference = self.ledger["reference"]

    def make(self, ledger):
        return KaggleExecution(self.account, self.root, ledger, lambda value: self.saved.append(copy.deepcopy(value)),
                               clock=lambda: self.now[0], sleep=lambda seconds: self.now.__setitem__(0, self.now[0] + seconds),
                               background_progress=False)

    def run_until(self, provider, seconds, on_status=None):
        stop_at = self.now[0] + seconds
        original = self.account.call
        def call(operation, **values):
            if operation == "status" and on_status:
                on_status()
            return original(operation, **values)
        self.account.call = call
        try:
            with self.assertRaises(TrainingCancelled):
                provider.run(lambda event: None, lambda: self.now[0] >= stop_at)
        finally:
            self.account.call = original


class GpuFallbackTests(Base):
    def test_queued_gpu_notebook_is_stopped_and_the_branch_runs_on_cpu(self):
        self.account.default_state = "QUEUED"
        provider = self.make(self.ledger)
        def cpu_runs_at_once():
            for reference, kernel in self.account.kernels.items():
                if reference != self.gpu_reference:
                    kernel["state"] = "RUNNING"
        self.run_until(provider, GPU_QUEUE_FALLBACK_SECONDS + 60, cpu_runs_at_once)
        gpu = self.account.kernels[self.gpu_reference]
        # One cancel signal on the queued GPU notebook, nothing else written to it.
        self.assertEqual(len(self.account.quick_saves(self.gpu_reference)), 1)
        self.assertTrue(source_control(gpu["source"])["cancel"])
        self.assertEqual(gpu["runs"], [{"version": 1, "gpu": True}])
        # The same branch, on the same uploaded input, now runs in a CPU notebook.
        cpu_reference = self.gpu_reference + "-c"
        self.assertEqual(provider.ledger["reference"], cpu_reference)
        cpu = self.account.kernels[cpu_reference]
        self.assertEqual(cpu["runs"], [{"version": 1, "gpu": False}])
        self.assertEqual(cpu["metadata"]["dataset_sources"], gpu["metadata"]["dataset_sources"])
        self.assertEqual(source_configuration(cpu["source"])["accelerator_override"], "cpu")
        self.assertEqual(provider.ledger["accelerator"], "cpu")
        fallback = provider.ledger["gpu_fallback"]
        self.assertGreaterEqual(fallback["after_seconds"], GPU_QUEUE_FALLBACK_SECONDS)
        self.assertEqual([item["reference"] for item in provider.ledger["abandoned_notebooks"]], [self.gpu_reference])
        self.assertEqual(sum(1 for op, values in self.account.calls if op == "dataset_create"), 1)
        # The Monitor learns that the branch moved to CPU, never the notebook names.
        from src.cloud_training.telemetry import timing_summary
        summary = timing_summary(provider.ledger)
        self.assertEqual(summary["gpu_fallback"], fallback)
        self.assertEqual(summary["accelerator"], "cpu")
        self.assertNotIn("abandoned_notebooks", summary)
        self.assertNotIn(self.gpu_reference, json.dumps(summary))

    def test_fallback_happens_once_and_never_before_the_wait(self):
        self.account.default_state = "QUEUED"
        provider = self.make(self.ledger)
        self.run_until(provider, GPU_QUEUE_FALLBACK_SECONDS - 10)
        self.assertNotIn("gpu_fallback", provider.ledger)
        self.assertEqual(self.account.quick_saves(), [])
        provider = self.make(self.saved[-1])
        # Both GPU and CPU stay queued: one fallback, then the normal queue limit applies.
        with self.assertRaises(ProviderError) as caught:
            provider.run(lambda event: None, lambda: False)
        self.assertEqual(caught.exception.category, "queue_timeout")
        self.assertEqual(len([r for r in self.account.kernels if r.startswith(self.gpu_reference)]), 2)

    def test_gpu_that_starts_in_time_is_kept(self):
        provider = self.make(self.ledger)
        self.run_until(provider, GPU_QUEUE_FALLBACK_SECONDS + 60)
        self.assertNotIn("gpu_fallback", provider.ledger)
        self.assertEqual(provider.ledger["accelerator"], "gpu")
        self.assertEqual(list(self.account.kernels), [self.gpu_reference])

    def test_cpu_branch_never_falls_back(self):
        ledger = copy.deepcopy(self.ledger)
        ledger["accelerator"] = "cpu"
        self.account.default_state = "QUEUED"
        provider = self.make(ledger)
        self.run_until(provider, GPU_QUEUE_FALLBACK_SECONDS + 60)
        self.assertNotIn("gpu_fallback", provider.ledger)
        self.assertEqual(self.account.quick_saves(), [])

    def test_cleanup_removes_the_abandoned_gpu_notebook_after_checking_its_identity(self):
        self.account.default_state = "QUEUED"
        provider = self.make(self.ledger)
        self.run_until(provider, GPU_QUEUE_FALLBACK_SECONDS + 30)
        self.account.kernels[provider.ledger["reference"]]["state"] = "COMPLETE"
        self.assertTrue(provider.stop())
        self.assertTrue(provider.cleanup())
        self.assertEqual(self.account.kernels, {})
        self.assertFalse(self.account.dataset)
        self.assertTrue(provider.ledger["abandoned_notebooks"][0]["notebook_cleaned"])

    def test_cleanup_refuses_an_abandoned_notebook_that_is_not_ours(self):
        self.account.default_state = "QUEUED"
        provider = self.make(self.ledger)
        self.run_until(provider, GPU_QUEUE_FALLBACK_SECONDS + 30)
        self.account.kernels[provider.ledger["reference"]]["state"] = "COMPLETE"
        self.account.kernels[self.gpu_reference]["source"] = self.account.kernels[self.gpu_reference]["source"].replace(
            "CONFIG = {", "CONFIG = {'x': 1, ", 1)
        self.assertTrue(provider.stop())
        with self.assertRaises(Exception):
            provider.cleanup()
        self.assertIn(self.gpu_reference, self.account.kernels)


class HardwareOverrideTests(unittest.TestCase):
    def test_override_only_downgrades_gpu_to_cpu(self):
        gpu = {"neural_accelerator": "gpu"}
        self.assertEqual(effective_accelerator(gpu, "lstm", {"TRAFFIC_KAGGLE_ACCELERATOR": "cpu"}), "cpu")
        self.assertEqual(effective_accelerator(gpu, "lstm", {}), "gpu")
        self.assertEqual(effective_accelerator({"neural_accelerator": "cpu"}, "gru",
                                               {"TRAFFIC_KAGGLE_ACCELERATOR": "gpu"}), "cpu")
        self.assertEqual(effective_accelerator(gpu, "tree", {"TRAFFIC_KAGGLE_ACCELERATOR": "cpu"}), "cpu")

    def test_cpu_notebook_name_requires_the_cpu_override(self):
        context = {**{key: str(uuid.uuid4()) for key in ("run_id", "region_id", "dataset_id", "attempt_id")},
                   "branch": "lstm", "configuration_sha256": "a" * 64}
        base = "probeuser/traffic-train-" + context["attempt_id"].replace("-", "")
        dataset = "probeuser/traffic-input-" + context["attempt_id"].replace("-", "")
        snapshot = {"context": context, "files": {}}
        with self.assertRaises(ValueError):
            render_notebook(snapshot, base + "-c", dataset)
        config = source_configuration(render_notebook(snapshot, base + "-c", dataset, accelerator_override="cpu"))
        self.assertEqual(config["accelerator_override"], "cpu")
        self.assertNotIn("accelerator_override", source_configuration(render_notebook(snapshot, base, dataset)))

    def test_supervisor_passes_the_override_to_the_training_process(self):
        template = (Path(__file__).resolve().parents[1] / "src/cloud_training/notebook.py").read_text(encoding="utf-8")
        self.assertIn('CONFIG.get("accelerator_override") == "cpu"', template)
        self.assertIn('environment["TRAFFIC_KAGGLE_ACCELERATOR"] = "cpu"', template)


class RunningLeaseTests(Base):
    def test_a_notebook_that_was_queued_keeps_a_valid_lease_for_a_long_run(self):
        self.ledger["accelerator"] = "cpu"   # no fallback; isolate the renewal schedule
        self.account.default_state = "QUEUED"
        provider = self.make(self.ledger)
        lapses = []
        def check():
            kernel = self.account.kernels[self.gpu_reference]
            if self.now[0] >= self.ledger_start + 120:
                kernel["state"] = "RUNNING"
            if kernel["state"] == "RUNNING" and source_control(kernel["source"])["lease_until"] <= self.now[0]:
                lapses.append(self.now[0])
        self.ledger_start = self.now[0]
        self.run_until(provider, 40 * 60, check)
        self.assertEqual(lapses, [])
        # Long leases: about one renewal per 10 minutes, not one per 45 seconds.
        self.assertLessEqual(len(self.account.quick_saves()), 5)
        self.assertGreaterEqual(len(self.account.quick_saves()), 3)

    def test_a_short_run_makes_no_quick_save(self):
        provider = self.make(self.ledger)
        self.run_until(provider, 5 * 60)
        self.assertEqual(self.account.quick_saves(), [])

    def test_renewals_write_a_long_lease(self):
        provider = self.make(self.ledger)
        self.run_until(provider, START_LEASE_SECONDS - RENEW_BEFORE_SECONDS + 30)
        self.assertEqual(len(self.account.quick_saves()), 1)
        lease = source_control(self.account.kernels[self.gpu_reference]["source"])["lease_until"]
        self.assertGreater(lease, self.now[0] + START_LEASE_SECONDS - 120)


if __name__ == "__main__":
    unittest.main()
