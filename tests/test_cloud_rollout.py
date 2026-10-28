import importlib.util
import ast
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from scripts.cloud_training.prepare_rollout import code_map, patch_for, sources
from scripts.cloud_training.manage_rollout import DEPLOYMENTS, Rollout
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class CloudRolloutTests(unittest.TestCase):
    def test_generated_read_only_inspection_script_compiles(self):
        rollout = object.__new__(Rollout)
        rollout.remote = Mock(side_effect=lambda name, source: {"source": source})
        source = rollout.inspect_run(str(uuid.uuid4()))["source"]
        ast.parse(source)

    def test_enable_checks_real_worker_and_api_health_contracts(self):
        rollout = object.__new__(Rollout)
        rollout.journal = {"stage_complete": True, "dag_was_paused": False}
        rollout.review = {"bundle_id": "reviewed"}
        rollout.snapshot = {"runtime": {"traffic-api": {"main_container": "api"}}}
        rollout.preflight = Mock()
        rollout.patch = Mock()
        rollout.pause = Mock()
        rollout.save = Mock()
        rollout.verify = Mock(return_value={"rollout_verified": True})
        def remote(name, source):
            if "doctor" in source:
                return {"authenticated": True}
            health = {"bundle_id": "reviewed", "cloud_enabled": True}
            if name == "traffic-api":
                health["execution_control"] = True
            return health
        rollout.remote = remote
        self.assertEqual(rollout.enable(), {"cloud_enabled": True, "default_backend": "local", "automatic_fallback": False})
        self.assertEqual(rollout.patch.call_count, 3)
        rollout.verify.assert_called_once_with(expected=True)
        rollout.pause.assert_called_with(False)

    def verification_fixture(self, directory, *, stale_second_api=False):
        rollout = object.__new__(Rollout)
        rollout.root = Path(directory)
        rollout.review = {"bundle_id": "reviewed"}
        rollout.journal = {"enabled": True}
        rollout.snapshot = {"deployments": {name: {"uid": name} for name in DEPLOYMENTS}}
        (rollout.root / "configmaps.json").write_text(json.dumps({"items": [{"metadata": {"name": "traffic-cloud-airflow-test"},
            "data": {"dag.py": "dag", "airflow_control.py": "helper", "legacy.py": "legacy"}}]}))
        (rollout.root / "airflow-source-proof.json").write_text(json.dumps({"task_graph": {}}))
        pods = [{"metadata": {"name": name + "-" + str(index), "labels": {"app": name}},
                 "status": {"phase": "Running", "containerStatuses": [{"ready": True}]}}
                for name in DEPLOYMENTS for index in range(2 if name == "traffic-api" else 1)]
        def call(*args, **kwargs):
            if args[:2] == ("get", "pods"):
                return {"items": pods}
            name = args[2]
            count = 2 if name == "traffic-api" else 1
            return {"metadata": {"uid": name, "generation": 1},
                    "spec": {"replicas": count, "selector": {"matchLabels": {"app": name}}},
                    "status": {"observedGeneration": 1, "updatedReplicas": count, "readyReplicas": count, "availableReplicas": count}}
        rollout.call = call
        rollout.remote = Mock(return_value=["20261028_01"])
        def remote(name, pod, source):
            if name.startswith("airflow"):
                return {"dag_source_and_graph_verified": True}
            return {"bundle_id": "stale" if stale_second_api and pod == "traffic-api-1" else "reviewed",
                    "cloud_enabled": True, "execution_control": True}
        rollout.remote_pod = Mock(side_effect=remote)
        return rollout

    def test_verify_checks_every_replica_without_starting_or_mutating_work(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = self.verification_fixture(directory)
            result = rollout.verify(expected=True)
        self.assertTrue(result["rollout_verified"])
        self.assertEqual(len(result["checked_pods"]), 6)
        self.assertFalse(result["training_started"])
        self.assertFalse(result["production_ready"])

    def test_verify_rejects_a_stale_second_api_replica(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = self.verification_fixture(directory, stale_second_api=True)
            with self.assertRaisesRegex(RuntimeError, "Runtime bundle/flags differ"):
                rollout.verify(expected=True)

    def test_existing_credential_secret_is_preserved_without_reading_token(self):
        rollout = object.__new__(Rollout)
        rollout.call = Mock(return_value="secret/traffic-kaggle-credentials")
        with patch("scripts.cloud_training.manage_rollout.Path.home", side_effect=AssertionError("Do not read local credential")):
            self.assertFalse(rollout.credentials()["created"])
        self.assertEqual(rollout.call.call_count, 1)

    def test_failed_run_cannot_pass_acceptance_until_stop_and_cleanup_are_confirmed(self):
        rollout = object.__new__(Rollout)
        result = {"state": "failed", "input_dataset_present": True, "active_model_from_this_run_count": 0,
                  "registered_model_count": 0, "branches": {"tree": {"started": True, "stop_confirmed": False, "cleanup_confirmed": True}}}
        rollout.remote = Mock(side_effect=lambda *args: json.loads(json.dumps(result)))
        run_id = str(uuid.uuid4())
        self.assertFalse(rollout.inspect_run(run_id, "failed")["acceptance_check_passed"])
        result["branches"]["tree"]["stop_confirmed"] = True
        self.assertTrue(rollout.inspect_run(run_id, "failed")["acceptance_check_passed"])
        result["registered_model_count"] = 1
        self.assertFalse(rollout.inspect_run(run_id, "failed")["acceptance_check_passed"])

    def test_completed_run_cannot_pass_acceptance_if_staging_is_still_present(self):
        rollout = object.__new__(Rollout)
        result = {"state": "completed", "input_dataset_present": True, "active_model_from_this_run_count": 0,
                  "registered_model_count": 1, "branches": {"tree": {"staging_cleaned": False}}}
        rollout.remote = Mock(side_effect=lambda *args: json.loads(json.dumps(result)))
        run_id = str(uuid.uuid4())
        self.assertFalse(rollout.inspect_run(run_id, "completed")["acceptance_check_passed"])
        result["branches"]["tree"]["staging_cleaned"] = True
        self.assertTrue(rollout.inspect_run(run_id, "completed")["acceptance_check_passed"])

    def test_patch_json_is_passed_as_a_real_temporary_file(self):
        rollout = object.__new__(Rollout)
        rollout.kubectl = "kubectl"
        paths = []
        def run(arguments, **kwargs):
            path = Path(next(item.removeprefix("--patch-file=") for item in arguments if item.startswith("--patch-file=")))
            paths.append(path)
            self.assertEqual(json.loads(path.read_text()), {"spec": {}})
            self.assertIsNone(kwargs["input"])
            return Mock(returncode=0, stdout="passed")
        with patch("scripts.cloud_training.manage_rollout.subprocess.run", side_effect=run):
            self.assertEqual(rollout.call("patch", "deployment", "worker", "--patch-file=-", content='{"spec":{}}'), "passed")
        self.assertFalse(paths[0].exists())

    def test_server_validation_never_applies_a_real_mutation(self):
        rollout = object.__new__(Rollout)
        with tempfile.TemporaryDirectory() as directory:
            rollout.root = Path(directory)
            for name in DEPLOYMENTS:
                (rollout.root / (name + ".patch.json")).write_text("{}")
            rollout.call = Mock()
            rollout.server_dry_run()
        self.assertEqual(rollout.call.call_count, 7)
        for call in rollout.call.call_args_list:
            self.assertIn("--dry-run=server", call.args)
            self.assertIn(call.args[0], {"apply", "patch"})

    def snapshot(self):
        return {"deployments": {"traffic-training-tree": {
            "containers": [{"name": "training-worker", "image": "base@sha256:" + "a" * 64,
                "ports": [{"containerPort": 8000}], "command": ["python", "outer.py"], "args": None,
                "safe_env": [], "volumeMounts": []}], "init_names": []}}}

    def test_worker_patch_preserves_image_and_leaves_cloud_disabled(self):
        code, items, manifest = code_map(sources())
        patch = patch_for(self.snapshot(), "traffic-training-tree", code, items, {})
        pod = patch["spec"]["template"]["spec"]
        main = pod["containers"][0]
        self.assertNotIn("image", main)
        flags = {item["name"]: item["value"] for item in main["env"]}
        self.assertEqual(flags["CLOUD_TRAINING_ENABLED"], "false")
        self.assertEqual(flags["TRAINING_CONTROL_ENABLED"], "false")
        self.assertTrue(any(item["name"] == "kaggle-credential" for item in pod["volumes"]))
        self.assertNotIn("KAGGLE_API_TOKEN", json.dumps(patch))
        self.assertEqual(pod["initContainers"][0]["image"], self.snapshot()["deployments"]["traffic-training-tree"]["containers"][0]["image"])

    def test_rollback_removes_only_owned_additions_and_restores_outer_entrypoint(self):
        code, items, manifest = code_map(sources())
        patch = patch_for(self.snapshot(), "traffic-training-tree", code, items, {}, rollback=True)
        pod = patch["spec"]["template"]["spec"]
        self.assertEqual(pod["containers"][0]["command"], ["python", "outer.py"])
        self.assertTrue(all(item["$patch"] == "delete" for item in pod["volumes"]))
        self.assertNotIn("replicas", patch["spec"])

    def test_code_bundle_is_hash_bound_and_fits_a_configmap(self):
        resource, items, manifest = code_map(sources())
        self.assertTrue(resource["immutable"])
        self.assertLess(len(json.dumps(resource).encode()), 900 * 1024)
        self.assertNotIn("backend.app.api.router", manifest["modules"])
        self.assertNotIn("backend.app.services.regions", manifest["modules"])
        self.assertIn("src.cloud_training.kaggle_rpc", manifest["modules"])
        spec = importlib.util.spec_from_file_location("cloud_runtime_contract_test", ROOT / "Deployment_test_on_rke2/runtime/cloud_training_runtime.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for item in items:
                path = root / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(resource["data"][item["key"]], encoding="utf-8", newline="")
            self.assertEqual(module.verify(root)["bundle_id"], manifest["bundle_id"])
            (root / "project/src/cloud_training/process.py").write_text("changed")
            with self.assertRaises(RuntimeError):
                module.verify(root)


if __name__ == "__main__":
    unittest.main()
