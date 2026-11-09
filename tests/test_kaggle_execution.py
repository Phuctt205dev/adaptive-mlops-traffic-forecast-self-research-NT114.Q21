import copy
import hashlib
import json
from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from src.cloud_training.control import CONTROL_PREFIX
from src.cloud_training.kaggle_execution import KaggleExecution, TrainingCancelled, initial_ledger
from src.cloud_training.kaggle_transport import ProviderError


class FakeStream:
    reader_failed = False
    def __init__(self, events):
        self.events = events
    def drain(self):
        events, self.events = self.events, []
        return events
    def returncode(self):
        return None
    def stop(self):
        return True


class FakeTransport:
    def __init__(self):
        self.calls, self.source, self.metadata = [], None, None
        self.version, self.state, self.lost_response = 0, "RUNNING", False
        self.resources = {"dataset": False, "notebook": False}
        self.events = []
        self.outputs = {}
    def call(self, operation, **values):
        self.calls.append((operation, values))
        if operation == "dataset_create":
            self.resources["dataset"] = True
            return {"created": True}
        if operation == "dataset_check":
            return {"private": True, "version": 1, "status": "ready"}
        if operation == "kernel_push":
            root = Path(values["directory"])
            self.source = (root / "train.py").read_text(encoding="utf-8")
            self.metadata = json.loads((root / "kernel-metadata.json").read_text())
            self.version += 1
            self.resources["notebook"] = True
            if self.lost_response:
                self.lost_response = False
                raise ProviderError("Response lost after provider accepted the write.")
            return {"version": self.version}
        if operation == "kernel_get":
            return {"source": self.source, "ref": self.metadata["id"], "current_version_number": self.version,
                    "is_private": True, "enable_gpu": self.metadata["enable_gpu"], "enable_internet": True,
                    "dataset_data_sources": self.metadata["dataset_sources"], "kernel_data_sources": [],
                    "competition_data_sources": [], "model_data_sources": []}
        if operation == "status":
            return {"state": self.state}
        if operation == "output_list":
            return {"files": {name: "https://provider.example/" + name for name in self.outputs}}
        if operation == "resource_exists":
            return {"exists": self.resources[values["kind"]]}
        if operation == "cleanup":
            if values.get("notebook"):
                self.resources["notebook"] = False
            if values.get("dataset"):
                self.resources["dataset"] = False
            return {"cleaned": True}
        raise AssertionError(operation)
    def stream(self, reference, context):
        return FakeStream(copy.deepcopy(self.events))


class KaggleExecutionTests(unittest.TestCase):
    def test_remote_cancel_is_latched_and_never_cleared_by_a_lease_renewal(self):
        from src.cloud_training.control import with_control, source_control
        self.provider.submit()
        self.transport.source = with_control(self.transport.source, cancel=True, clock=1000)
        self.provider.control()
        self.assertTrue(source_control(self.transport.source)["cancel"])
        self.assertTrue(self.provider.ledger["cancel_requested"])

    def test_foreign_control_attempt_is_rejected_before_quick_save(self):
        self.provider.submit()
        # Change just the mutable control line, leaving the pinned body/config intact.
        lines = self.transport.source.splitlines()
        for index, line in enumerate(lines):
            if line.startswith(CONTROL_PREFIX):
                value = json.loads(line[len(CONTROL_PREFIX):])
                value["attempt_id"] = str(uuid.uuid4())
                lines[index] = CONTROL_PREFIX + json.dumps(value)
        self.transport.source = "\n".join(lines) + "\n"
        before = sum(operation == "kernel_push" for operation, _ in self.transport.calls)
        with self.assertRaises(ValueError):
            self.provider.control()
        self.assertEqual(sum(operation == "kernel_push" for operation, _ in self.transport.calls), before)

    def test_progress_delivery_failure_always_closes_the_stream_and_blocks_fetch(self):
        stream = FakeStream([])
        stream.stop = Mock(return_value=True)
        self.provider.stream = stream
        self.provider.submit = Mock()
        self.provider._progress_error = ProviderError("Invalid progress stream")
        self.provider.fetch = Mock()
        with self.assertRaises(ProviderError):
            self.provider.run(Mock(), lambda: False)
        stream.stop.assert_called_once()
        self.provider.fetch.assert_not_called()

    def test_transient_control_refusal_retries_only_after_unchanged_version_readback(self):
        self.provider.submit()
        original, writes = self.transport.call, []
        def call(operation, **values):
            if operation == "kernel_push":
                writes.append(values)
                if len(writes) == 1:
                    return {"rejected": True, "diagnostic": {"reason": "rate_limit", "retryable": True}}
            return original(operation, **values)
        self.transport.call = call
        self.provider.control()
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(write["no_run"] for write in writes))
        self.assertFalse(self.provider.ledger["control_pending"])
        self.assertEqual(self.provider.ledger["version"], 1)

    def test_unknown_rejection_is_diagnosed_and_not_retried(self):
        self.provider.submit()
        original, writes = self.transport.call, []
        def call(operation, **values):
            if operation == "kernel_push":
                writes.append(values)
                return {"rejected": True, "diagnostic": {"reason": "provider_rejected", "raw": "SECRET"}}
            return original(operation, **values)
        self.transport.call = call
        with self.assertRaises(ProviderError) as caught:
            self.provider.control()
        self.assertEqual(caught.exception.category, "control_rejected")
        self.assertEqual(len(writes), 1)
        self.assertNotIn("SECRET", json.dumps(self.provider.ledger))
        self.assertFalse(self.provider.ledger["control_pending"])

    def test_refusal_with_invalid_sources_remains_fatal_even_if_flag_says_retryable(self):
        self.provider.submit()
        original, writes = self.transport.call, []
        def call(operation, **values):
            if operation == "kernel_push":
                writes.append(values)
                return {"rejected": True, "diagnostic": {"reason": "rate_limit", "retryable": True,
                    "invalid_sources": ["invalid_dataset_sources"]}}
            return original(operation, **values)
        self.transport.call = call
        with self.assertRaises(ProviderError):
            self.provider.control()
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.provider.ledger["last_failure"]["diagnostic"]["reason"], "invalid_sources")

    def test_rejected_flag_can_be_reconciled_only_with_advanced_version_and_exact_source(self):
        self.provider.submit()
        original = self.transport.call
        def call(operation, **values):
            response = original(operation, **values)
            return {"rejected": True} if operation == "kernel_push" else response
        self.transport.call = call
        self.provider.control()
        self.assertEqual(self.provider.ledger["control_version"], 2)
        self.assertEqual(self.provider.ledger["confirmed_control_writes"], 1)

    def test_uncertain_write_and_restart_never_repeat_an_unconfirmed_control(self):
        self.provider.submit()
        original, writes = self.transport.call, []
        def call(operation, **values):
            if operation == "kernel_push":
                writes.append(values)
                raise ProviderError("response not received", operation=operation, category="response_timeout")
            return original(operation, **values)
        self.transport.call = call
        with self.assertRaises(ProviderError):
            self.provider.control()
        recovered = self.make(self.saved[-1])
        with self.assertRaises(ProviderError):
            recovered.control(cancel=True)
        self.assertEqual(len(writes), 1)
        self.assertTrue(recovered.ledger["control_pending"])

    def test_restart_reconciles_the_original_control_intent_before_a_new_write(self):
        self.provider.submit()
        original = self.transport.call
        written = [False]
        def call(operation, **values):
            if operation == "kernel_get" and written[0]:
                raise ProviderError("readback unavailable", operation=operation)
            response = original(operation, **values)
            if operation == "kernel_push":
                written[0] = True
                raise ProviderError("reply unavailable", operation=operation, category="response_timeout")
            return response
        self.transport.call = call
        with self.assertRaises(ProviderError):
            self.provider.control()
        recovered = self.make(self.saved[-1])
        self.transport.call = original
        recovered.control()
        self.assertFalse(recovered.ledger["control_pending"])
        self.assertEqual(recovered.ledger["confirmed_control_writes"], 1)
        self.assertEqual(sum(op == "kernel_push" for op, _ in self.transport.calls), 2)  # submit + original control

    def test_rpc_operation_timing_uses_one_monotonic_clock_and_records_failed_calls(self):
        self.provider.submit()
        clock = [0.0]
        self.provider.monotonic = lambda: clock[0]
        original = self.transport.call
        def call(operation, **values):
            clock[0] += 7
            if operation == "status":
                raise ProviderError("not confirmed")
            return original(operation, **values)
        self.transport.call = call
        with self.assertRaises(ProviderError):
            self.provider.status()
        measured = self.provider.ledger["telemetry"]["operations"]["status"]
        self.assertEqual(measured["seconds"], 7)
        self.assertEqual(measured["errors"], 1)
        self.assertNotIn("provider_running_observed", self.provider.ledger["telemetry"]["milestones"])

    def test_progress_is_delivered_while_control_rpc_is_blocked(self):
        self.provider.submit()
        blocked, delivered = threading.Event(), threading.Event()
        class LiveStream(FakeStream):
            def drain(stream):
                return super().drain() if blocked.is_set() else []
        stream = LiveStream([{"sequence": 1, "model": "random_forest_lag", "stage": "model_started"}])
        self.transport.stream = lambda *args: stream
        original = self.transport.call
        def call(operation, **values):
            if operation == "kernel_push":
                blocked.set()
                self.assertTrue(delivered.wait(2), "ML progress was blocked by the control RPC")
            return original(operation, **values)
        self.transport.call = call
        self.provider.background_progress = True
        try:
            self.provider._start_progress(lambda event: delivered.set())
            self.provider.control()
        finally:
            self.provider._stop_progress()
        self.assertEqual(self.provider.last_sequence, 1)
        self.assertFalse(self.provider._progress_thread.is_alive())

    def test_lost_control_reply_reconciles_source_without_another_write(self):
        self.provider.submit()
        self.transport.lost_response = True
        self.provider.control()
        pushes = [values for op, values in self.transport.calls if op == "kernel_push"]
        self.assertEqual([item["no_run"] for item in pushes], [False, True])
        self.assertFalse(self.provider.ledger["control_pending"])
        self.assertEqual(self.provider.ledger["confirmed_control_writes"], 1)
        self.assertEqual(self.provider.ledger["version"], 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "input").mkdir()
        self.context = {**{key: str(uuid.uuid4()) for key in ("run_id", "region_id", "dataset_id", "attempt_id")},
                        "branch": "tree", "configuration_sha256": "a" * 64}
        self.ledger = initial_ledger({"context": self.context, "files": {}}, "probeuser")
        self.saved, self.transport = [], FakeTransport()
        self.provider = self.make(self.ledger)
    def make(self, ledger):
        return KaggleExecution(self.transport, self.root, ledger, lambda value: self.saved.append(copy.deepcopy(value)),
                               clock=lambda: 1000, sleep=lambda seconds: None, background_progress=False)

    def test_submit_pins_private_version_and_fences_before_network(self):
        original = self.transport.call
        def fenced(operation, **values):
            if operation == "dataset_create":
                self.assertTrue(self.saved[-1]["dataset_pending"])
            return original(operation, **values)
        self.transport.call = fenced
        self.assertTrue(self.provider.submit().endswith("/1"))
        self.assertTrue(any(item["dataset_pending"] for item in self.saved))
        self.assertTrue(any(item["submit_pending"] for item in self.saved))
        self.assertEqual(self.transport.metadata["dataset_sources"], [self.ledger["dataset_reference"] + "/1"])
        self.assertTrue(self.transport.metadata["is_private"])
        self.assertFalse(self.transport.metadata["enable_gpu"])

    def test_lost_submit_response_reconciles_without_a_second_fit(self):
        self.transport.lost_response = True
        with self.assertRaises(ProviderError):
            self.provider.submit()
        recovered = self.make(self.saved[-1])
        recovered.submit()
        pushes = [values for op, values in self.transport.calls if op == "kernel_push"]
        self.assertEqual(len(pushes), 1)
        self.assertFalse(pushes[0]["no_run"])
        self.assertEqual(recovered.ledger["version"], 1)

    def test_cancel_quick_save_never_claims_stopped_until_original_terminal(self):
        self.provider.submit()
        self.assertFalse(self.provider.stop())
        signal = json.loads([line[len(CONTROL_PREFIX):] for line in self.transport.source.splitlines()
                             if line.startswith(CONTROL_PREFIX)][0])
        self.assertTrue(signal["cancel"])
        self.assertEqual(self.provider.reference, self.ledger["reference"] + "/1")
        self.assertEqual(self.transport.version, 2)
        self.assertFalse(self.provider.ledger["remote_stopped"])
        self.assertFalse(self.provider.cleanup())
        self.transport.state = "COMPLETE"
        self.assertTrue(self.provider.stop())
        self.assertTrue(self.provider.cleanup())
        self.assertFalse(any(self.transport.resources.values()))
        self.assertTrue(all(values["no_run"] for op, values in self.transport.calls if op == "kernel_push" and Path(values["directory"]).name == "control"))

    def test_changed_remote_source_cannot_be_controlled_or_deleted(self):
        self.provider.submit()
        self.transport.source = self.transport.source.replace("time.sleep(3)", "time.sleep(30)")
        with self.assertRaises(ValueError):
            self.provider.control(cancel=True)
        self.assertFalse(any(op == "cleanup" for op, _ in self.transport.calls))

    def test_cleanup_reconciles_a_lost_delete_response(self):
        self.provider.submit()
        self.provider.save(remote_stopped=True, notebook_delete_pending=True)
        self.transport.resources["notebook"] = False
        self.assertTrue(self.provider.cleanup())
        self.assertTrue(self.provider.ledger["notebook_cleaned"])

    def test_completed_output_requires_manifest_and_binds_all_downloads(self):
        self.provider.submit()
        self.provider.save(remote_stopped=True, terminal="COMPLETE")
        model = b"synthetic, never unpickled"
        manifest = {"schema": 1, "context": self.context,
                    "result": {"model_file": {"$training_file": "model.pkl"}},
                    "files": [{"path": "model.pkl", "size": len(model), "sha256": hashlib.sha256(model).hexdigest()}]}
        self.transport.outputs = {"output/manifest.json": json.dumps(manifest).encode(), "output/model.pkl": model,
                                  "ignored-provider.log": b"do not download"}
        downloaded = []
        def download(url, path, **options):
            name = url.removeprefix("https://provider.example/")
            downloaded.append(name)
            path.write_bytes(self.transport.outputs[name])
        self.provider.downloader = download
        result = self.provider.fetch()
        self.assertEqual(result["model_file"], str(self.root / "output/model.pkl"))
        self.assertEqual(downloaded, ["output/manifest.json", "output/model.pkl"])

    def test_missing_final_manifest_has_a_bounded_wait_without_another_fit(self):
        self.provider.submit()
        self.provider.save(remote_stopped=True, terminal="COMPLETE")
        with self.assertRaises(ProviderError) as caught:
            self.provider.fetch()
        self.assertEqual(caught.exception.category, "file_not_ready")
        self.assertEqual(sum(op == "output_list" for op, _ in self.transport.calls), 12)
        self.assertEqual(sum(op == "kernel_push" for op, _ in self.transport.calls), 1)

    def output_fixture(self, names):
        self.provider.submit()
        self.provider.save(remote_stopped=True, terminal="COMPLETE")
        payloads = {name: ("synthetic:" + name).encode() for name in names}
        manifest = {"schema": 1, "context": self.context,
                    "result": {"verified": True, "files": [{"$training_file": name} for name in names]},
                    "files": [{"path": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                              for name, body in payloads.items()]}
        self.transport.outputs = {"output/manifest.json": json.dumps(manifest).encode(),
                                  **{"output/" + name: body for name, body in payloads.items()}}
        return manifest

    def test_output_pool_overlaps_at_most_three_files_and_closes_sessions(self):
        self.output_fixture([f"model-{n}.bin" for n in range(6)])
        active, peak, lock, barrier, sessions = [0], [0], threading.Lock(), threading.Barrier(3), []
        def session():
            instance = Mock()
            sessions.append(instance)
            return instance
        def download(url, path, **options):
            name = url.removeprefix("https://provider.example/")
            if name != "output/manifest.json":
                self.assertIsNotNone(options["session"])
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                try:
                    barrier.wait(timeout=3)
                    self.assertEqual(options["refresh_url"](), url)
                    path.write_bytes(self.transport.outputs[name])
                finally:
                    with lock:
                        active[0] -= 1
            else:
                path.write_bytes(self.transport.outputs[name])
        self.provider.downloader = download
        with patch("requests.Session", side_effect=session):
            result = self.provider.fetch()
            self.assertTrue(result["verified"])
            self.assertEqual(len(result["files"]), 6)
        self.assertEqual(peak[0], 3)
        self.assertEqual(active[0], 0)
        self.assertEqual(len(sessions), 3)
        for instance in sessions:
            instance.close.assert_called_once()
        operations = self.provider.ledger["telemetry"]["operations"]
        self.assertEqual(operations["output_download"]["count"], 6)
        self.assertEqual(operations["output_transfer_wall"]["count"], 1)
        self.assertEqual({values["reference"] for op, values in self.transport.calls if op == "output_list"},
                         {self.provider.reference})

    def test_output_failure_drains_pool_before_verification_and_return(self):
        self.output_fixture([f"model-{n}.bin" for n in range(6)])
        barrier, exited, lock = threading.Barrier(3), [], threading.Lock()
        def download(url, path, **options):
            name = url.removeprefix("https://provider.example/")
            if name == "output/manifest.json":
                path.write_bytes(self.transport.outputs[name])
                return
            try:
                barrier.wait(timeout=3)
                if name.endswith("0.bin"):
                    raise ProviderError("Synthetic checksum failure", category="checksum_mismatch")
                # Another receiver must see the shared stop fence promptly.
                for _ in range(2000):
                    if options["cancelled"]():
                        raise TrainingCancelled("Sibling transfer failed.")
                    threading.Event().wait(.001)
                raise AssertionError("Transfer did not observe the stop fence")
            finally:
                with lock:
                    exited.append(name)
        self.provider.downloader = download
        with patch("src.cloud_training.kaggle_execution.read_bundle") as verify:
            with self.assertRaises(ProviderError) as caught:
                self.provider.fetch()
        self.assertEqual(caught.exception.category, "checksum_mismatch")
        verify.assert_not_called()
        self.assertGreaterEqual(len(exited), 3)
        self.assertFalse(any(t.name.startswith("kaggle-output") for t in threading.enumerate()))

    def test_manifest_rejects_duplicate_or_colliding_destinations_before_payload_download(self):
        manifest = self.output_fixture(["model.bin"])
        original = manifest["files"][0]
        for names in (["model.bin", "model.bin"], ["folder", "folder/model.bin"],
                      ["manifest.json"], ["manifest.json/model.bin"]):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as root:
                self.provider.directory = Path(root)
                manifest["files"] = [{**original, "path": name} for name in names]
                self.transport.outputs["output/manifest.json"] = json.dumps(manifest).encode()
                def download(url, path, **options):
                    self.assertTrue(str(url).endswith("/manifest.json"))
                    path.write_bytes(self.transport.outputs["output/manifest.json"])
                self.provider.downloader = Mock(side_effect=download)
                with self.assertRaises(ProviderError):
                    self.provider.fetch()
                self.assertEqual(self.provider.downloader.call_count, 1)

    def queued_clock(self):
        elapsed = [1000]
        self.provider.clock = self.provider.monotonic = lambda: elapsed[0]
        self.provider.sleep = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
        self.transport.state = "QUEUED"
        return elapsed

    def test_long_queue_reduces_status_calls_while_renewing_the_actual_lease(self):
        elapsed = self.queued_clock()
        with self.assertRaises(TrainingCancelled):
            self.provider.run(lambda _: None, lambda: elapsed[0] >= 1120)
        statuses = [values["reference"] for op, values in self.transport.calls if op == "status"]
        self.assertGreater(len(statuses), 5)
        self.assertLess(len(statuses), 16)  # Previously ~40 over this fake 120s queue.
        self.assertEqual(set(statuses), {self.provider.reference})
        pushes = [values for op, values in self.transport.calls if op == "kernel_push"]
        self.assertEqual(sum(not item["no_run"] for item in pushes), 1)
        self.assertGreaterEqual(sum(item["no_run"] for item in pushes), 3)
        self.assertEqual(elapsed[0], 1120)
        self.assertGreater(self.provider.ledger["lease_until"], 1120)

    def test_queued_backoff_is_interrupted_by_real_ml_events(self):
        elapsed = self.queued_clock()
        observations = []
        original = self.transport.call
        def call(operation, **values):
            if operation == "status":
                observations.append(elapsed[0])
            return original(operation, **values)
        self.transport.call = call
        class LiveStream(FakeStream):
            def drain(stream):
                if elapsed[0] == 1022:
                    stream.events = [{"sequence": 1}]
                return super().drain()
        self.transport.stream = lambda *args: LiveStream([])
        with self.assertRaises(TrainingCancelled):
            self.provider.run(lambda _: None, lambda: elapsed[0] >= 1024)
        self.assertIn(1022, observations)

    def terminal_run(self, event_after=None):
        self.transport.state = 'COMPLETE'
        elapsed = [0]
        self.provider.clock = lambda: elapsed[0]
        self.provider.sleep = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
        self.provider.fetch = Mock(return_value={'verified': True})

        class DelayedStream(FakeStream):
            def drain(stream):
                if event_after is not None and elapsed[0] >= event_after and not stream.events:
                    stream.events = [{'control': 'child_completed'}]
                return super().drain()

        self.transport.stream = lambda *args: DelayedStream([])
        return elapsed

    def test_complete_status_waits_for_delayed_final_event_without_resubmitting(self):
        elapsed = self.terminal_run(event_after=12)
        result = self.provider.run(lambda event: None, lambda: False)
        self.assertEqual(result, {'verified': True})
        self.assertGreaterEqual(elapsed[0], 12)
        self.assertEqual(sum(op == 'dataset_create' for op, _ in self.transport.calls), 1)
        self.assertEqual(sum(op == 'kernel_push' and not value.get('no_run')
                             for op, value in self.transport.calls), 1)
        self.assertFalse(any(op == "kernel_push" and value.get("no_run") for op, value in self.transport.calls))

    def test_complete_without_final_event_remains_failed_and_bounded(self):
        elapsed = self.terminal_run()
        with self.assertRaises(ProviderError) as caught:
            self.provider.run(lambda event: None, lambda: False)
        self.assertEqual(caught.exception.operation, 'terminal_events')
        self.assertEqual(caught.exception.category, 'completion_unconfirmed')
        self.assertLessEqual(elapsed[0], 90)
        self.provider.fetch.assert_not_called()

    def test_cancel_during_terminal_event_wait_never_fetches_output(self):
        elapsed = self.terminal_run(event_after=12)
        with self.assertRaises(TrainingCancelled):
            self.provider.run(lambda event: None, lambda: elapsed[0] >= 5)
        self.provider.fetch.assert_not_called()

    def test_failed_child_is_rejected_even_with_complete_status_and_completion_event(self):
        self.terminal_run()
        self.transport.stream = lambda *args: FakeStream([
            {'control': 'child_completed'}, {'control': 'failed'},
        ])
        with self.assertRaises(ProviderError):
            self.provider.run(lambda event: None, lambda: False)
        self.provider.fetch.assert_not_called()

    def test_invalid_final_stream_is_not_accepted_as_completion(self):
        self.terminal_run()
        stream = FakeStream([{'control': 'child_completed'}])
        stream.reader_failed = True
        self.transport.stream = lambda *args: stream
        with self.assertRaises(ProviderError):
            self.provider.run(lambda event: None, lambda: False)
        self.provider.fetch.assert_not_called()

    def test_stream_exit_preserves_events_queued_during_reader_join(self):
        class FinishingStream(FakeStream):
            def returncode(stream):
                stream.events.append({'control': 'child_completed'})
                return 0
        self.provider.stream = FinishingStream([])
        self.provider.drain(lambda event: None)
        self.assertTrue(self.provider.ledger.get('child_completed_observed'))


if __name__ == "__main__":
    unittest.main()
