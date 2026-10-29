"""Check real runner boundaries with fake ML; never fit or call a provider."""
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from scripts.cloud_training import execute_branch as runner
from src.cloud_training.package import configuration_digest, ml_sources


class NeuralShapIntegrationTests(unittest.TestCase):
    def execute(self, branch, backend, options, status="completed"):
        configuration = {"selected_models": [branch], "training_backend": backend, "shap": options}
        context = {"run_id": str(uuid.uuid4()), "region_id": str(uuid.uuid4()), "dataset_id": str(uuid.uuid4()),
                   "branch": branch, "attempt_id": str(uuid.uuid4()), "configuration_sha256": configuration_digest(configuration)}
        candidate = {"shap_report": {"status": status}}
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "synthetic.csv"
            data.write_bytes(b"a,b\n1,2\n")
            job = {"context": context, "configuration": configuration, "dataset_sha256": hashlib.sha256(data.read_bytes()).hexdigest()}
            fake_tf = SimpleNamespace(config=SimpleNamespace(list_physical_devices=lambda kind: ["synthetic GPU"]))
            with patch.dict(sys.modules, {"tensorflow": fake_tf}), patch.object(runner, "prepare_runtime"), patch.object(runner, "report"), \
                    patch("src.region_training_variants.train_region_recurrent_candidate", return_value={
                        "status": "completed", "candidates": [candidate]}) as train, patch.object(runner, "write_bundle") as publish:
                runner.run(job, data, Path(directory) / "output")
                self.assertEqual(train.call_args.kwargs["config"]["shap"], options)
                self.assertEqual(train.call_args.kwargs["model_name"], branch.upper())
                publish.assert_called_once()

    def test_local_and_cloud_honor_neural_shap_for_both_models(self):
        for branch in ("lstm", "gru"):
            for backend in ("local", "kaggle"):
                with self.subTest(branch=branch, backend=backend):
                    self.execute(branch, backend, {"enabled": True, "max_samples": 2})

    def test_explicit_disable_is_preserved(self):
        self.execute("lstm", "local", {"enabled": False}, "disabled")

    def test_enabled_but_failed_shap_does_not_publish_a_successful_bundle(self):
        with self.assertRaises(RuntimeError):
            self.execute("gru", "local", {"enabled": True}, "failed")

    def test_snapshot_contains_the_neural_explainer_and_runner(self):
        sources = ml_sources(Path(__file__).resolve().parents[1])
        self.assertIn("src/shap_reporting.py", sources)
        self.assertIn("src/models/recurrent.py", sources)
        self.assertIn("scripts/cloud_training/execute_branch.py", sources)


if __name__ == "__main__":
    unittest.main()
