import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from src.cloud_training.control import CONTROL_PREFIX
from src.cloud_training.kaggle_execution import KaggleExecution, initial_ledger
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
                               clock=lambda: 1000, sleep=lambda seconds: None)

    def test_submit_pins_private_version_and_fences_before_network(self):
        self.assertTrue(self.provider.submit().endswith("/1"))
        self.assertTrue(self.saved[0]["dataset_pending"])
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


if __name__ == "__main__":
    unittest.main()
