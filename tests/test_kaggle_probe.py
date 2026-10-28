import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock
from concurrent.futures import ThreadPoolExecutor

from src.cloud_training.kaggle_probe_client import KaggleProbeClient
from src.cloud_training.probe import (
    EVENT_PREFIX, OUTPUT_FILES, ProbeError, load_job, parse_event,
    prepare_probe, safe_status, separate_sdk_log, update_job, verify_output, versioned_reference, write_json,
)


class KaggleProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory, self.job = prepare_probe(self.root, "truongphucdev")
        self.api = Mock()
        self.api.kernels_push.return_value = SimpleNamespace(
            ref=self.job["reference"], version_number=7, kernel_id=123, error="",
            invalid_tags=[], invalid_dataset_sources=[], invalid_competition_sources=[],
            invalid_kernel_sources=[], invalid_model_sources=[])
        self.api.kernels_status.return_value = SimpleNamespace(status="RUNNING", failure_message="")
        self.client = KaggleProbeClient(self.api, kernel_request_factory=SimpleNamespace)
        self.remote_response = self.remote_metadata(self.directory, self.job, version=7)

    def remote_metadata(self, directory, job, version=7):
        metadata = SimpleNamespace(ref=job["reference"], is_private=True, current_version_number=version,
                                   id=123, enable_gpu=False, enable_internet=job["purpose"] == "cooperative-cancel", kernel_type="script", language="python",
                                   dataset_data_sources=[], kernel_data_sources=[], competition_data_sources=[], model_data_sources=[])
        response = SimpleNamespace(metadata=metadata, blob=SimpleNamespace(
            source=(directory / "probe.py").read_text(encoding="utf-8")))
        self.api.build_kaggle_client.return_value = MagicMock()
        self.api.build_kaggle_client.return_value.__enter__.return_value.kernels.kernels_api_client.get_kernel.return_value = response
        return response

    def make_outputs(self):
        root = self.directory / "output"
        root.mkdir()
        for name in OUTPUT_FILES:
            (root / name).write_bytes(b"small-test-payload")
        write_json(root / "report.json", {"job_id": self.job["job_id"], "status": "completed", "gpu_count": 0, "synthetic_only": True})
        write_json(root / "preprocessor.json", {"schema": 1, "kind": "identity", "sequence_length": 6, "features": 2})
        files = [{"path": name, "size": (root / name).stat().st_size,
                  "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
                 for name in sorted(OUTPUT_FILES)]
        manifest = {"schema": 1, "job_id": self.job["job_id"], "files": files}
        write_json(root / "manifest.json", manifest)
        return root, manifest

    def event(self, **updates):
        return EVENT_PREFIX + json.dumps({"schema": 1, "job_id": self.job["job_id"], "seq": 1,
                                         "stage": "epoch_completed", "epoch": 1, "epochs": 3, **updates})

    def test_prepare_is_private_synthetic_and_offline(self):
        metadata = json.loads((self.directory / "kernel-metadata.json").read_text())
        self.assertIs(metadata["is_private"], True)
        self.assertFalse(metadata["enable_internet"])
        self.assertEqual(metadata["dataset_sources"], [])
        self.assertFalse(self.job["submitted"])
        self.assertNotIn("token", (self.directory / "probe.py").read_text().lower())
        compile((self.directory / "probe.py").read_text(), "probe.py", "exec")
        self.api.kernels_push.assert_not_called()

    def test_modified_source_cannot_be_submitted(self):
        with (self.directory / "probe.py").open("a") as stream:
            stream.write("\nprint('modified')")
        with self.assertRaises(ProbeError):
            self.client.submit(self.directory)
        self.api.kernels_push.assert_not_called()

    def test_modified_privacy_cannot_be_submitted(self):
        metadata = json.loads((self.directory / "kernel-metadata.json").read_text())
        metadata["is_private"] = False
        write_json(self.directory / "kernel-metadata.json", metadata)
        with self.assertRaises(ProbeError):
            self.client.submit(self.directory)

    def test_username_and_accelerator_are_validated(self):
        for username in ("../other", "name/slug", "bad name", ""):
            with self.subTest(username=username), self.assertRaises(ProbeError):
                prepare_probe(self.root, username)
        with self.assertRaises(ProbeError):
            prepare_probe(self.root, "truongphucdev", "unknown")
        for duration in (-1, 31, True):
            with self.subTest(duration=duration), self.assertRaises(ProbeError):
                prepare_probe(self.root, "truongphucdev", observation_seconds=duration)

    def test_successful_submit_is_pinned_and_cannot_run_twice(self):
        result = self.client.submit(self.directory)
        self.assertEqual(result["reference"], self.job["reference"] + "/7")
        self.assertFalse(result["automatic_cancellation_ready"])
        with self.assertRaises(ProbeError):
            self.client.submit(self.directory)
        self.assertEqual(self.api.kernels_push.call_count, 1)

    def test_uncertain_submit_is_fenced_before_network(self):
        self.api.kernels_push.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.client.submit(self.directory)
        _, job = load_job(self.directory)
        self.assertTrue(job["submission_pending"])
        with self.assertRaises(ProbeError):
            self.client.submit(self.directory)

    def test_concurrent_submissions_have_only_one_network_owner(self):
        def attempt():
            try:
                self.client.submit(self.directory)
                return True
            except ProbeError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: attempt(), range(4)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.api.kernels_push.call_count, 1)

    def test_existing_submission_lock_survives_uncertain_process_restart(self):
        (self.directory / "submission.lock").write_text(self.job["job_id"])
        with self.assertRaises(ProbeError):
            self.client.submit(self.directory)
        self.api.kernels_push.assert_not_called()

    def test_progress_update_does_not_overwrite_a_concurrent_cancel(self):
        update_job(self.directory, {"cooperative_cancel_requested": True, "last_event_seq": 20})
        result = update_job(self.directory, {"progress_events_observed": True, "last_event_seq": 5})
        self.assertTrue(result["cooperative_cancel_requested"])
        self.assertEqual(result["last_event_seq"], 20)

    def test_cooperative_probe_is_cpu_only_and_uses_no_project_endpoint(self):
        with self.assertRaises(ProbeError):
            prepare_probe(self.root, "truongphucdev", "gpu", "cooperative-cancel")
        directory, job = prepare_probe(self.root, "truongphucdev", purpose="cooperative-cancel")
        metadata = json.loads((directory / "kernel-metadata.json").read_text())
        self.assertTrue(metadata["is_private"])
        self.assertTrue(metadata["enable_internet"])
        self.assertFalse(metadata["enable_gpu"])
        source = (directory / "probe.py").read_text(encoding="utf-8")
        self.assertNotIn("ngrok", source)
        self.assertNotIn("INTERNAL_TRAINING_TOKEN", source)
        compile(source, "cooperative-probe.py", "exec")

    def test_cooperative_cancel_quick_saves_without_starting_another_run(self):
        directory, job = prepare_probe(self.root, "truongphucdev", purpose="cooperative-cancel")
        self.api.kernels_push.return_value.ref = job["reference"]
        remote = self.remote_metadata(directory, job)
        self.client.submit(directory)
        self.api.kernels_push.reset_mock()

        def save_control(folder, *, no_run):
            self.assertIs(no_run, True)
            remote.blob.source = (Path(folder) / "probe.py").read_text(encoding="utf-8")
            remote.metadata.current_version_number = 8
            return SimpleNamespace(error="")

        self.api.kernels_push.side_effect = save_control
        result = self.client.cooperative_cancel(directory)
        self.assertTrue(result["cancel_signal_published"])
        self.assertFalse(result["cancel_confirmed"])
        self.assertFalse(result["new_training_run_started"])
        self.assertEqual(self.api.kernels_push.call_count, 1)
        self.client.cooperative_cancel(directory)  # A repeated request is read-only.
        self.assertEqual(self.api.kernels_push.call_count, 1)
        self.assertEqual(versioned_reference(load_job(directory)[1]), job["reference"] + "/7")

    def test_cooperative_cancel_rejects_changed_remote_source(self):
        directory, job = prepare_probe(self.root, "truongphucdev", purpose="cooperative-cancel")
        self.api.kernels_push.return_value.ref = job["reference"]
        remote = self.remote_metadata(directory, job)
        self.client.submit(directory)
        remote.blob.source += "\n# unexpected remote update"
        self.api.kernels_push.reset_mock()
        with self.assertRaises(ProbeError):
            self.client.cooperative_cancel(directory)
        self.api.kernels_push.assert_not_called()

    def test_cooperative_cancel_needs_child_observation_ack_and_terminal_state(self):
        directory, job = prepare_probe(self.root, "truongphucdev", purpose="cooperative-cancel")
        self.api.kernels_push.return_value.ref = job["reference"]
        self.remote_metadata(directory, job)
        self.client.submit(directory)
        self.api.kernels_status.return_value.status = "COMPLETE"
        update_job(directory, {"cooperative_cancel_requested": True})
        self.assertFalse(self.client.confirm_cancel(directory)["cancel_confirmed"])
        update_job(directory, {"cooperative_child_running_observed": True, "cooperative_cancel_ack_observed": True})
        self.assertTrue(self.client.confirm_cancel(directory)["cancel_confirmed"])
        self.api.kernels_status.return_value.status = "RUNNING"
        self.assertFalse(self.client.confirm_cancel(directory)["cancel_confirmed"])

    def test_wrong_remote_ref_is_not_success(self):
        self.remote_response.metadata.ref = "someone/else"
        with self.assertRaises(ProbeError):
            self.client.submit(self.directory)
        self.assertFalse(load_job(self.directory)[1]["submitted"])

    def test_noncanonical_response_ref_requires_remote_source_verification(self):
        self.api.kernels_push.return_value.ref = "not-an-owner-slug"
        self.assertTrue(self.client.submit(self.directory)["source_and_privacy_verified"])

    def test_response_version_must_match_remote_version(self):
        self.remote_response.metadata.current_version_number = 8
        with self.assertRaises(ProbeError):
            self.client.submit(self.directory)

    def test_response_session_id_is_accepted_only_from_exact_provider_link(self):
        for url in ("https://attacker.invalid/code/" + self.job["reference"] + "?kernelSessionId=999",
                    "https://www.kaggle.com/code/other/notebook?kernelSessionId=999",
                    "https://www.kaggle.com/code/" + self.job["reference"] + "?kernelSessionId=999&kernelSessionId=1000"):
            self.assertIsNone(self.client.response_session_id(SimpleNamespace(url=url), self.job["reference"]))
        self.api.kernels_push.return_value.url = "https://www.kaggle.com/code/" + self.job["reference"] + "?kernelSessionId=999"
        self.assertTrue(self.client.submit(self.directory)["automatic_cancellation_ready"])
        self.assertEqual(load_job(self.directory)[1]["session_id"], 999)

    def remote_for_pending_submission(self):
        self.api.kernels_push.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.client.submit(self.directory)
        return self.remote_metadata(self.directory, self.job, version=1)

    def test_reconcile_reads_exact_source_without_submitting_again(self):
        self.remote_for_pending_submission()
        result = self.client.reconcile(self.directory, request_factory=SimpleNamespace)
        self.assertTrue(result["source_and_privacy_verified"])
        self.assertEqual(result["reference"], self.job["reference"] + "/1")
        self.assertEqual(self.api.kernels_push.call_count, 1)

    def test_reconcile_rejects_public_or_different_remote_source(self):
        response = self.remote_for_pending_submission()
        for field, value in (("is_private", False), ("enable_internet", True), ("enable_gpu", True), ("ref", "other/notebook")):
            previous = getattr(response.metadata, field)
            setattr(response.metadata, field, value)
            with self.subTest(field=field), self.assertRaises(ProbeError):
                self.client.reconcile(self.directory, request_factory=SimpleNamespace)
            setattr(response.metadata, field, previous)
        response.blob.source += "\nprint('different')"
        with self.assertRaises(ProbeError):
            self.client.reconcile(self.directory, request_factory=SimpleNamespace)
        self.assertTrue(load_job(self.directory)[1]["submission_pending"])

    def test_status_uses_exact_version_not_latest(self):
        self.client.submit(self.directory)
        self.assertEqual(self.client.status(self.directory)["state"], "RUNNING")
        self.api.kernels_status.assert_called_once_with(self.job["reference"] + "/7")

    def test_unknown_provider_state_is_rejected(self):
        with self.assertRaises(ProbeError):
            safe_status("MAGIC_SUCCESS")

    def test_progress_is_filtered_and_run_scoped(self):
        self.assertIsNone(parse_event("arbitrary provider output", self.job))
        self.assertEqual(parse_event(self.event(), self.job)["epoch"], 1)
        with self.assertRaises(ProbeError):
            parse_event(self.event(job_id="other-run"), self.job)
        with self.assertRaises(ProbeError):
            parse_event(self.event(token="do-not-log"), self.job)

    def test_nonfinite_metrics_are_rejected(self):
        with self.assertRaises(ProbeError):
            parse_event(self.event(loss=float("nan")), self.job)

    def test_stream_has_exact_job_version_and_only_structured_events(self):
        self.client.submit(self.directory)
        self.api.kernels_logs_stream.return_value = [{"data": "raw message\n" + self.event()}]
        events = list(self.client.events(self.directory))
        self.assertEqual(len(events), 1)
        self.api.kernels_logs_stream.assert_called_once_with(self.job["reference"] + "/7")

    def test_verify_never_claims_cpu_reload_or_production_readiness(self):
        root, _ = self.make_outputs()
        report = verify_output(root, self.job)
        self.assertTrue(report["output_verified"])
        self.assertFalse(report["cpu_reload_verified"])
        self.assertFalse(report["production_ready"])

    def test_sdk_log_is_separated_without_accepting_unexpected_model_files(self):
        root, _ = self.make_outputs()
        logfile = root / (self.job["reference"].split("/")[1] + ".log")
        logfile.write_text("owned notebook diagnostic")
        with self.assertRaises(ProbeError):
            verify_output(root, self.job)
        separate_sdk_log(self.directory, self.job)
        self.assertEqual((self.directory / "provider.log").read_text(), "owned notebook diagnostic")
        self.assertTrue(verify_output(root, self.job)["output_verified"])

    def test_sdk_log_does_not_overwrite_existing_diagnostic(self):
        root, _ = self.make_outputs()
        logfile = root / (self.job["reference"].split("/")[1] + ".log")
        logfile.write_text("new")
        (self.directory / "provider.log").write_text("existing")
        with self.assertRaises(ProbeError):
            separate_sdk_log(self.directory, self.job)
        self.assertEqual((self.directory / "provider.log").read_text(), "existing")

    def test_corrupted_same_size_output_is_rejected(self):
        root, _ = self.make_outputs()
        path = root / "model.keras"
        path.write_bytes(b"X" * path.stat().st_size)
        with self.assertRaises(ProbeError):
            verify_output(root, self.job)

    def test_cross_run_manifest_is_rejected(self):
        root, manifest = self.make_outputs()
        manifest["job_id"] = "other-run"
        write_json(root / "manifest.json", manifest)
        with self.assertRaises(ProbeError):
            verify_output(root, self.job)

    def test_path_traversal_and_duplicate_paths_are_rejected(self):
        root, manifest = self.make_outputs()
        for name in ("../model.keras", "C:/model.keras", manifest["files"][1]["path"]):
            manifest["files"][0]["path"] = name
            write_json(root / "manifest.json", manifest)
            with self.subTest(path=name), self.assertRaises(ProbeError):
                verify_output(root, self.job)

    def test_gpu_request_requires_gpu_evidence(self):
        root, _ = self.make_outputs()
        with self.assertRaises(ProbeError):
            verify_output(root, {**self.job, "accelerator": "gpu"})

    def test_output_limits_and_unexpected_files_are_rejected(self):
        root, manifest = self.make_outputs()
        (root / "unexpected.txt").write_text("extra")
        with self.assertRaises(ProbeError):
            verify_output(root, self.job)
        (root / "unexpected.txt").unlink()
        manifest["files"][0]["size"] = 10 * 1024 * 1024
        write_json(root / "manifest.json", manifest)
        with self.assertRaises(ProbeError):
            verify_output(root, self.job)

    def test_cannot_download_running_or_failed_job(self):
        self.client.submit(self.directory)
        for state in ("RUNNING", "ERROR", "CANCEL_ACKNOWLEDGED"):
            self.api.kernels_status.return_value.status = state
            with self.subTest(state=state), self.assertRaises(ProbeError):
                self.client.fetch(self.directory)
        self.api.kernels_output.assert_not_called()

    def test_cancel_does_not_guess_session_from_notebook_id(self):
        directory, job = prepare_probe(self.root, "truongphucdev", purpose="cancel")
        self.api.kernels_push.return_value.ref = job["reference"]
        self.remote_metadata(directory, job)
        self.client.submit(directory)
        self.api.build_kaggle_client.reset_mock()
        reference = versioned_reference(load_job(directory)[1])
        for session in (None, 0, 123, 999):
            with self.subTest(session=session), self.assertRaises(ProbeError):
                self.client.cancel(directory, session, reference)
        self.api.build_kaggle_client.assert_not_called()

    def test_cancel_acknowledgement_is_not_inferred_from_error_or_complete(self):
        self.client.submit(self.directory)
        _, job = load_job(self.directory)
        job["cancel_requested"] = True
        write_json(self.directory / "job.json", job)
        for state in ("CANCEL_REQUESTED", "ERROR", "COMPLETE"):
            self.api.kernels_status.return_value.status = state
            self.assertFalse(self.client.confirm_cancel(self.directory)["cancel_confirmed"])
        self.api.kernels_status.return_value.status = "CANCEL_ACKNOWLEDGED"
        result = self.client.confirm_cancel(self.directory)
        self.assertTrue(result["cancel_confirmed"])
        self.assertFalse(result["production_ready"])


if __name__ == "__main__":
    unittest.main()
