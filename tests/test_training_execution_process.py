import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid

from src.cloud_training.artifacts import ArtifactError, read_bundle, write_bundle
from src.cloud_training.process import ExecutionProcess, PREFIX, cleanup_execution, execution_directory


class ExecutionProcessTests(unittest.TestCase):
    def test_stop_really_ends_the_child_and_filters_raw_output(self):
        context = {"run_id": str(uuid.uuid4()), "attempt_id": str(uuid.uuid4())}
        event = {"context": context, "event": {"sequence": 1, "model": "lstm", "stage": "model_started"}}
        source = "import time;print('do not retain raw diagnostics',flush=True);print(" + repr(PREFIX + json.dumps(event)) + ",flush=True);time.sleep(60)"
        process = ExecutionProcess([sys.executable, "-u", "-c", source], context)
        self.addCleanup(process.stop)
        deadline, observed = time.monotonic() + 5, []
        while not observed and time.monotonic() < deadline:
            observed = process.drain()
            time.sleep(.02)
        self.assertEqual(observed, [event["event"]])
        self.assertIsNone(process.returncode())
        self.assertTrue(process.stop())
        self.assertIsNotNone(process.returncode())

    def test_different_run_progress_is_not_accepted(self):
        context = {"run_id": "expected"}
        event = {"context": {"run_id": "other"}, "event": {"sequence": 1}}
        process = ExecutionProcess([sys.executable, "-u", "-c", "print(" + repr(PREFIX + json.dumps(event)) + ")"], context)
        self.addCleanup(process.stop)
        process.process.wait(timeout=5)
        process.reader.join(timeout=2)
        self.assertTrue(process.reader_failed)
        self.assertEqual(process.drain(), [])

    def test_cleanup_is_scoped_to_one_attempt_and_never_the_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "staging"
            run, attempt = str(uuid.uuid4()), str(uuid.uuid4())
            target = execution_directory(root, run, "tree", attempt)
            target.mkdir(parents=True)
            (target / "partial-model").write_bytes(b"partial")
            preserved = root / "keep-this-other-run"
            preserved.mkdir()
            self.assertTrue(cleanup_execution(root, run, "tree", attempt))
            self.assertTrue(preserved.exists())
            with self.assertRaises(ValueError):
                cleanup_execution(root, "..", "tree", attempt)
            with self.assertRaises(ValueError):
                execution_directory(root, run, "../", attempt)

    def test_artifact_roundtrip_rebinds_paths_and_clears_offline_mlflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "model.pkl"
            file.write_bytes(b"synthetic serialization, never deserialized")
            context = {"run_id": "run", "configuration_sha256": "hash"}
            result = {"model_file": str(file), "mlflow_run_id": "offline-run", "mlflow_model_uri": "runs:/offline-run/model"}
            write_bundle(result, context, root)
            received = read_bundle(root, context)
            self.assertEqual(received["model_file"], str(file))
            self.assertIsNone(received["mlflow_model_uri"])
            with self.assertRaises(ArtifactError):
                read_bundle(root, {"run_id": "wrong", "configuration_sha256": "hash"})
            file.write_bytes(b"X" * file.stat().st_size)
            with self.assertRaises(ArtifactError):
                read_bundle(root, context)

    def test_artifact_cannot_reference_source_outside_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "staging"
            root.mkdir()
            outside = Path(directory) / "private"
            outside.write_text("never publish")
            with self.assertRaises(ArtifactError):
                write_bundle({"model_file": str(outside)}, {}, root)

    def test_manifest_cannot_smuggle_an_unbound_artifact_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_bundle({"branch": "tree", "artifact_paths": []}, {}, root)
            manifest["result"]["artifact_paths"] = ["/private/unverified.pkl"]
            (root / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ArtifactError):
                read_bundle(root, {})


if __name__ == "__main__":
    unittest.main()
