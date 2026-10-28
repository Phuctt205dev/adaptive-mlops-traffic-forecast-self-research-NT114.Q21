import ast
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
import zipfile

from src.cloud_training.control import CONTROL_PREFIX, body_digest, render_notebook, source_configuration, with_control
from src.cloud_training.package import ml_sources, prepare_package
from src.cloud_training.probe import canonical, sha256_file


ROOT = Path(__file__).resolve().parents[1]


class TrainingPackageTests(unittest.TestCase):
    def context(self, config):
        return {**{key: str(uuid.uuid4()) for key in ("run_id", "region_id", "dataset_id", "attempt_id")},
                "branch": "tree", "configuration_sha256": hashlib.sha256(canonical(config)).hexdigest()}

    def test_source_closure_is_pure_ml_and_does_not_archive_the_workspace(self):
        sources = ml_sources(ROOT)
        self.assertIn("src/models/random_forest.py", sources)
        self.assertIn("src/models/recurrent.py", sources)
        self.assertIn("src/shap_reporting.py", sources)
        self.assertTrue(all(name.endswith(".py") for name in sources))
        self.assertFalse(any(name.startswith(("backend/", "Deployment", "tests/", ".")) for name in sources))
        self.assertNotIn("src/cloud_training/kaggle_probe_client.py", sources)

    def test_package_binds_dataset_configuration_and_source_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "input.csv"
            dataset.write_text("a,b\n1,2\n")
            config = {"selected_models": ["random_forest_lag"], "training_backend": "kaggle"}
            context = self.context(config)
            snapshot = prepare_package(ROOT, dataset, config, context, root / "input", sha256_file(dataset))
            self.assertEqual(set(snapshot["files"]), {"source.bin", "dataset.csv", "job.json"})
            with zipfile.ZipFile(root / "input/source.bin") as archive:
                self.assertEqual(set(archive.namelist()), set(ml_sources(ROOT)))
            for name, descriptor in snapshot["files"].items():
                self.assertEqual(sha256_file(root / "input" / name), descriptor["sha256"])
            with self.assertRaises(ValueError):
                prepare_package(ROOT, dataset, {**config, "changed": True}, context, root / "wrong", sha256_file(dataset))
            with self.assertRaises(ValueError):
                prepare_package(ROOT, dataset, config, context, root / "wrong", "0" * 64)

    def test_control_updates_preserve_exact_execution_and_do_not_create_code(self):
        context = self.context({})
        attempt = context["attempt_id"].replace("-", "")
        snapshot = {"context": context, "files": {}}
        source = render_notebook(snapshot, "user/traffic-train-" + attempt, "user/traffic-input-" + attempt, clock=1000)
        ast.parse(source)
        renewed = with_control(source, clock=1100)
        cancelled = with_control(renewed, cancel=True, clock=1200)
        self.assertEqual(body_digest(source), body_digest(cancelled))
        self.assertEqual(source_configuration(source), source_configuration(cancelled))
        controls = [json.loads(line[len(CONTROL_PREFIX):]) for line in cancelled.splitlines() if line.startswith(CONTROL_PREFIX)]
        self.assertEqual(controls, [{"attempt_id": context["attempt_id"], "cancel": True, "lease_until": 1380}])
        with self.assertRaises(ValueError):
            with_control(source.replace("time.sleep(3)", "time.sleep(300)"))
        with self.assertRaises(ValueError):
            render_notebook(snapshot, "user/an-existing-notebook", "user/some-dataset")


if __name__ == "__main__":
    unittest.main()
