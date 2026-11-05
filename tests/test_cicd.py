"""Delivery safety and immutable packaging; no live services or credentials."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from scripts.cicd import deploy, github_gate, maintenance, release, startup

COMMIT = "a" * 40
IMAGE = "ghcr.io/example/traffic/backend@sha256:" + "b" * 64


def fixture(root):
    baseline = {"application_base_image": "example.com/base@sha256:" + "c" * 64,
                "application_deployments": ["traffic-training-tree", "traffic-training-worker", "traffic-api"],
                "airflow_deployments": ["airflow-scheduler", "airflow-webserver"], "migration_heads": ["20261028_01"]}
    files = {"backend/app/main.py": b"value = 1\n", "src/cloud_training/airflow_control.py": b"value = 2\n",
             "src/feature_selection.py": b"value = 3\n", "app.py": b"value = 4\n",
             "scripts/cloud_training/execute_branch.py": b"value = 5\n", "scripts/cloud_training/verify_execution.py": b"value = 6\n",
             "scripts/cicd/maintenance.py": b"value = 7\n", "docs/index.html": b"<html>fixture</html>"}
    for name in release.RUNTIME_FILES:
        files["deployment/rke2/runtime/" + name] = b"value = 8\n"
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    path = root / "deployment/rke2/baseline.json"
    path.write_text(json.dumps(baseline))
    return files


def deployment(name="traffic-api"):
    airflow = name.startswith("airflow-")
    main = {"name": name, "image": "original@sha256:" + "c" * 64,
            "command": ["python", "-B", "/opt/traffic-cloud/cloud_training_runtime.py"],
            "volumeMounts": [{"name": "cloud-code", "mountPath": "/opt/traffic-cloud", "readOnly": True}],
            "envFrom": [{"secretRef": {"name": "application-secret"}}],
            "ports": [{"containerPort": 8000}] if not airflow else []}
    return {"metadata": {"name": name, "uid": name + "-uid", "resourceVersion": "7"},
            "spec": {"replicas": 2 if name == "traffic-api" else 1, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}, "annotations": {"existing": "keep"}},
                                  "spec": {"containers": [main, {"name": "sidecar", "image": "untouched"}],
                                           "volumes": [{"name": "cloud-code", "configMap": {"name": "old-runtime"}},
                                                       {"name": "artifact-cache", "emptyDir": {"sizeLimit": "4Gi"}}],
                                           "initContainers": [{"name": "restore", "image": "restore-image"}],
                                           "nodeSelector": {"worker": "existing"}}}}}


def apply_patch(document, operations):
    result = copy.deepcopy(document)
    for operation in operations:
        parts = [p.replace("~1", "/").replace("~0", "~") for p in operation["path"].split("/")[1:]]
        parent = result
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
        if operation["op"] == "remove":
            del parent[key]
        elif isinstance(parent, list) and operation["op"] == "add":
            parent.insert(key, copy.deepcopy(operation["value"]))
        else:
            parent[key] = copy.deepcopy(operation["value"])
    return result


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.files = fixture(self.root)
        self.output = self.root / "release"

    def build(self):
        return release.build(self.root, self.output, COMMIT, tracked=self.files)

    def test_explicit_context_excludes_private_files_even_if_present(self):
        (self.root / ".env").write_text("SECRET=must-not-ship")
        (self.root / "private-review.json").write_text("private")
        document = self.build()
        self.assertEqual(release.verify(self.output, expected_commit=COMMIT), document)
        self.assertNotIn(b"must-not-ship", b"".join(p.read_bytes() for p in self.output.rglob("*") if p.is_file()))
        self.assertIn("payload/maintenance.py", document["files"])
        self.assertIn("payload/cloud/project/docs/index.html", document["files"])
        self.assertIn("FROM example.com/base@sha256:", (self.output / "Dockerfile").read_text())
        self.assertIn('RUN ["python", "-B", "/opt/traffic-release/maintenance.py", "--check-imports"]',
                      (self.output / "Dockerfile").read_text())

    def test_missing_tracked_runtime_fails_clean_checkout(self):
        self.files.pop("deployment/rke2/runtime/cloud_training_runtime.py")
        with self.assertRaisesRegex(ValueError, "tracked"):
            self.build()

    def test_dependency_change_requires_a_separate_reviewed_upgrade(self):
        path = self.root / 'requirements/backend.txt'
        path.parent.mkdir(parents=True)
        path.write_text('new dependency')
        baseline_path = self.root / 'deployment/rke2/baseline.json'
        baseline = json.loads(baseline_path.read_text())
        baseline['dependency_contract'] = {'requirements/backend.txt': '0' * 64}
        baseline_path.write_text(json.dumps(baseline))
        with self.assertRaisesRegex(ValueError, 'Dependency/schema contract changed'):
            self.build()

    def test_changed_file_extra_file_and_wrong_commit_are_rejected(self):
        self.build()
        with self.assertRaisesRegex(ValueError, "different commit"):
            release.verify(self.output, expected_commit="d" * 40)
        extra = self.output / ".env"
        extra.write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            release.verify(self.output)
        extra.unlink()
        (self.output / "payload/cloud/project/app.py").write_text("changed")
        with self.assertRaisesRegex(ValueError, "hash"):
            release.verify(self.output)

    def test_metadata_tampering_and_unsafe_paths_fail(self):
        self.build()
        path = self.output / "release.json"
        document = json.loads(path.read_text())
        document["commit"] = "d" * 40
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "metadata"):
            release.verify(self.output)
        for name in ("../secret", "/secret", "C:/secret", "a\\secret", "a/../b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.safe_path(name)

    def test_bundle_identity_binds_full_runtime(self):
        document = self.build()
        manifest = json.loads((self.output / "payload/cloud/manifest.json").read_text())
        identity = {k: v for k, v in manifest.items() if k != "bundle_id"}
        self.assertEqual(release.digest(release.canonical(identity)), document["cloud_bundle_id"])
        self.assertIn("backend.app.main", manifest["modules"])
        self.assertIn("app", manifest["modules"])

    def test_overlay_loads_new_parent_package_without_replacing_existing_base_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / 'legacy'; (legacy / 'src').mkdir(parents=True)
            (legacy / 'src/__init__.py').write_text("origin='legacy-package'\n")
            (legacy / 'src/existing.py').write_text("origin='old-leaf'\n")
            cloud = root / 'cloud'; package = cloud / 'project/src/cloud_training'; package.mkdir(parents=True)
            (package / '__init__.py').write_text("origin='reviewed-new-package'\n")
            (package / 'contracts.py').write_text("origin='reviewed-contracts'\n")
            (cloud / 'project/src/existing.py').write_text("origin='reviewed-leaf'\n")
            runtime = Path(__file__).resolve().parents[1] / 'deployment/rke2/runtime/cloud_training_runtime.py'
            document = {'modules': {'src.cloud_training.contracts': 'project/src/cloud_training/contracts.py',
                                    'src.existing': 'project/src/existing.py'},
                        'files': {'project/src/cloud_training/__init__.py': 'checked'}}
            script = """import importlib.util,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import src
spec=importlib.util.spec_from_file_location('runtime',sys.argv[2]);runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
runtime.install(json.loads(sys.argv[4]),Path(sys.argv[3]))
from src.cloud_training import contracts
import src.existing
print(json.dumps({'package':src.origin,'new_package':sys.modules['src.cloud_training'].origin,'contracts':contracts.origin,'leaf':src.existing.origin}))
try: import src.unreviewed
except ModuleNotFoundError: pass
else: raise AssertionError('Unknown module must not be synthesized')
"""
            result = subprocess.run([sys.executable, '-B', '-c', script, str(legacy), str(runtime), str(cloud), json.dumps(document)],
                                    cwd=root, text=True, encoding='utf-8', capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'package': 'legacy-package', 'new_package': 'reviewed-new-package',
                                                         'contracts': 'reviewed-contracts', 'leaf': 'reviewed-leaf'})


class PatchTests(unittest.TestCase):
    def test_only_runtime_image_and_metadata_change_and_inverse_restores_exactly(self):
        before = deployment()
        forward, inverse = deploy.release_patch(before, IMAGE, "r" * 64)
        after = apply_patch(before, forward)
        pod = after["spec"]["template"]["spec"]
        self.assertEqual(pod["containers"][1], before["spec"]["template"]["spec"]["containers"][1])
        self.assertEqual(pod["volumes"][1], before["spec"]["template"]["spec"]["volumes"][1])
        self.assertEqual(pod["initContainers"][0]["name"], "delivery-source")
        self.assertEqual(pod["initContainers"][1]["name"], "restore")
        self.assertEqual(after["spec"]["replicas"], 2)
        self.assertEqual(apply_patch(after, inverse), before)
        self.assertNotIn("envFrom", json.dumps(inverse))

    def test_airflow_keeps_dependency_image_and_receives_dag_payload(self):
        before = deployment("airflow-scheduler")
        forward, inverse = deploy.release_patch(before, IMAGE, "r")
        after = apply_patch(before, forward)
        pod = after["spec"]["template"]["spec"]
        self.assertEqual(pod["containers"][0]["image"], before["spec"]["template"]["spec"]["containers"][0]["image"])
        self.assertIn("/airflow", pod["initContainers"][0]["command"][-1])
        self.assertEqual(apply_patch(after, inverse), before)

    def test_repeat_patch_is_idempotent_and_rollback_preserves_first_release(self):
        before = deployment()
        first, _ = deploy.release_patch(before, IMAGE, "first", pull_secret="registry")
        current = apply_patch(before, first)
        repeat, _ = deploy.release_patch(current, IMAGE, "first", pull_secret="registry")
        self.assertEqual(apply_patch(current, repeat), current)
        second, inverse = deploy.release_patch(current, IMAGE.replace("b" * 64, "d" * 64), "second", pull_secret="registry")
        self.assertEqual(apply_patch(apply_patch(current, second), inverse), current)

    def test_missing_init_annotations_and_pull_secret_restore(self):
        before = deployment()
        del before["spec"]["template"]["spec"]["initContainers"]
        del before["spec"]["template"]["metadata"]["annotations"]
        operations, inverse = deploy.release_patch(before, IMAGE, "r", pull_secret="registry")
        self.assertEqual(apply_patch(apply_patch(before, operations), inverse), before)

    def test_wrong_entrypoint_or_secret_volume_is_rejected(self):
        before = deployment()
        before["spec"]["template"]["spec"]["containers"][0]["command"] = ["uvicorn", "app:app"]
        with self.assertRaisesRegex(RuntimeError, "Entrypoint"):
            deploy.release_patch(before, IMAGE, "r")
        before = deployment()
        before["spec"]["template"]["spec"]["volumes"][0] = {"name": "cloud-code", "secret": {"secretName": "private"}}
        with self.assertRaisesRegex(RuntimeError, "volume"):
            deploy.release_patch(before, IMAGE, "r")

    def test_terminal_and_terminating_pods_never_count_as_ready(self):
        before = deployment()
        good = {"metadata": {"name": "ready", "labels": {"app": "traffic-api"}},
                "status": {"phase": "Running", "containerStatuses": [{"ready": True}]}}
        failed = copy.deepcopy(good); failed["status"]["phase"] = "Failed"
        ending = copy.deepcopy(good); ending["metadata"]["deletionTimestamp"] = "now"
        self.assertEqual(deploy.ready_pods(before, [failed, ending, good]), [good])


class GateTests(unittest.TestCase):
    def responses(self):
        return [{"conclusion": "success", "status": "completed", "event": "push", "workflow_id": 1,
                 "head_branch": "master", "head_sha": COMMIT, "head_repository": {"full_name": "owner/repo"}},
                {"id": 1}, {"default_branch": "master"},
                {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"id": 1}]}]}]

    def test_trusted_push_and_real_review_gate(self):
        result = github_gate.validate("owner/repo", "1", fetch=Mock(side_effect=self.responses()))
        self.assertEqual(result["sha"], COMMIT)
        self.assertEqual(result["artifact"], "backend-release-" + COMMIT)

    def test_no_reviewers_fails_before_self_hosted_job(self):
        values = self.responses(); values[-1] = {"protection_rules": []}
        with self.assertRaisesRegex(ValueError, "reviewers"):
            github_gate.validate("owner/repo", "1", fetch=Mock(side_effect=values))

    def test_pr_failed_wrong_workflow_branch_and_foreign_repo_are_rejected(self):
        for field, value in (("event", "pull_request"), ("conclusion", "failure"), ("workflow_id", 2),
                             ("head_branch", "feature"), ("head_repository", {"full_name": "attacker/repo"})):
            values = self.responses(); values[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                github_gate.validate("owner/repo", "1", fetch=Mock(side_effect=values))


class StartupTests(unittest.TestCase):
    def test_only_complete_identity_and_gate_proof_can_allow_rollout(self):
        release_document = {"cloud_bundle_id": "checked-bundle", "commit": COMMIT, "release_id": "checked-release"}
        proof = {"source_startup_verified": True, "execution_gate_verified": True, "release_health_verified": True,
                 "lifespan_not_started": True, "fit_called": False, "deployment_changed": False,
                 "bundle_id": "checked-bundle", "commit": COMMIT, "release_id": "checked-release"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'payload/cloud').mkdir(parents=True)
            cluster = Mock(); cluster.pod.return_value = 'existing-pod'
            cluster.call.return_value = 'startup log\nTRAFFIC_DELIVERY_STARTUP ' + json.dumps(proof)
            accepted = startup.check(cluster, root, release_document, 'traffic-api', deployment(), {'name': 'traffic-api'})
            self.assertTrue(accepted['execution_gate_verified'])
            for key, value in (('execution_gate_verified', False), ('bundle_id', 'different-source'), ('fit_called', True)):
                with self.subTest(key=key):
                    cluster.call.return_value = 'TRAFFIC_DELIVERY_STARTUP ' + json.dumps({**proof, key: value})
                    with self.assertRaisesRegex(RuntimeError, 'before rollout'):
                        startup.check(cluster, root, release_document, 'traffic-api', deployment(), {'name': 'traffic-api'})


class DeliveryTests(unittest.TestCase):
    def operator(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name); files = fixture(root)
        release.build(root, root / "package", COMMIT, tracked=files)
        cluster = Mock()
        operator = deploy.Delivery(cluster, root / "package", IMAGE, root / "journal.json", commit=COMMIT)
        values = {name: deployment(name) for name in operator.names}
        operator.inventory = Mock(return_value=(values, {"dag_paused": False}))
        operator.check_startup = Mock(return_value=[])
        plans = {}
        for name, before in values.items():
            forward, inverse = deploy.release_patch(before, IMAGE, operator.release["release_id"])
            after = apply_patch(before, forward)
            plans[name] = {"uid": before["metadata"]["uid"], "before": deploy.spec_hash(before), "after": deploy.spec_hash(after), "patch": forward, "inverse": inverse}
        operator.plan = Mock(return_value=(values, {"dag_paused": False}, plans))
        cluster.deployment.side_effect = lambda name: copy.deepcopy(values[name])
        def mutate(before, operations, **kwargs):
            values[before["metadata"]["name"]] = apply_patch(before, operations)
        cluster.patch.side_effect = mutate
        operator.start_guard = Mock(side_effect=lambda api: operator.save(guard_held=True))
        operator.guard_status = Mock(return_value={"state": "held"})
        operator.release_guard = Mock(side_effect=lambda: operator.save(guard_held=False))
        operator.pause = Mock()
        operator.health = Mock(return_value=[{"verified": True}])
        return operator, values

    def test_approved_release_updates_roles_once_and_restores_pause_state(self):
        operator, _ = self.operator()
        self.assertTrue(operator.apply()["completed"])
        self.assertEqual(operator.cluster.patch.call_count, 5)
        self.assertEqual(operator.pause.call_args_list[-1].args, (False,))
        self.assertFalse(operator.state["guard_held"])
        self.assertTrue(operator.apply()["up_to_date"])
        self.assertEqual(operator.cluster.patch.call_count, 5)

    def test_failed_rollout_restores_exact_before_specs(self):
        operator, values = self.operator()
        originals = copy.deepcopy(values)
        calls = 0
        def wait(name):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("rollout failed")
        operator.cluster.wait.side_effect = wait
        with self.assertRaisesRegex(RuntimeError, "rollout failed"):
            operator.apply()
        self.assertEqual(values, originals)
        self.assertTrue(operator.state["rolled_back"])
        self.assertFalse(operator.state["recovery_required"])

    def test_busy_preflight_makes_no_mutation(self):
        operator, _ = self.operator()
        operator.inventory.side_effect = RuntimeError("Training active")
        with self.assertRaisesRegex(RuntimeError, "Training active"):
            operator.apply()
        operator.start_guard.assert_not_called()
        operator.cluster.patch.assert_not_called()

    def test_candidate_startup_failure_stops_before_guard_pause_and_patch(self):
        operator, _ = self.operator()
        operator.check_startup.side_effect = RuntimeError("Candidate startup/gates failed before rollout")
        with self.assertRaisesRegex(RuntimeError, "Candidate startup"):
            operator.apply()
        operator.start_guard.assert_not_called()
        operator.pause.assert_not_called()
        operator.cluster.patch.assert_not_called()
        self.assertFalse(operator.state.get("started"))

    def test_external_change_stops_instead_of_overwriting_it(self):
        operator, values = self.operator()
        def acquire(api):
            operator.save(guard_held=True)
            values[operator.names[0]]["spec"]["replicas"] = 3
        operator.start_guard.side_effect = acquire
        with self.assertRaisesRegex(RuntimeError, "changed after review"):
            operator.apply()
        operator.cluster.patch.assert_not_called()
        self.assertEqual(values[operator.names[0]]["spec"]["replicas"], 3)

    def test_ambiguous_patch_response_is_reconciled_and_rolled_back(self):
        operator, values = self.operator()
        originals = copy.deepcopy(values)
        first = True
        def mutate(before, operations, **kwargs):
            nonlocal first
            values[before["metadata"]["name"]] = apply_patch(before, operations)
            if first:
                first = False
                raise RuntimeError("response lost")
        operator.cluster.patch.side_effect = mutate
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            operator.apply()
        self.assertEqual(values, originals)
        self.assertTrue(operator.state["rolled_back"])

    def test_lost_guard_prevents_patch_and_marks_recovery(self):
        operator, _ = self.operator()
        operator.guard_status.return_value = {"state": "failed"}
        with self.assertRaisesRegex(RuntimeError, "guard was lost"):
            operator.apply()
        operator.cluster.patch.assert_not_called()
        self.assertTrue(operator.state["recovery_required"])

    def test_interrupted_transaction_is_not_replayed(self):
        operator, _ = self.operator()
        operator.state["started"] = True
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            operator.apply()
        operator.inventory.assert_not_called()

    def test_guard_exit_between_running_and_exec_reports_safe_startup_cause(self):
        operator, _ = self.operator()
        running = {"metadata": {"name": "guard-pod"}, "status": {"phase": "Running", "containerStatuses": [
            {"name": "guard", "state": {"running": {"startedAt": "now"}}}]}}
        failed = copy.deepcopy(running)
        failed["status"].update(phase="Failed", containerStatuses=[{"name": "guard", "state": {"terminated": {"exitCode": 1}}}])
        operator.cluster.call.side_effect = [{}, {"items": [running]}, {"items": [failed]},
            json.dumps({"maintenance": "failed", "phase": "imports", "error_type": "ModuleNotFoundError", "private": "must-not-log"})]
        operator.guard_status.side_effect = RuntimeError("container already exited")
        with patch.object(deploy.time, "sleep"), self.assertRaisesRegex(RuntimeError, "phase=imports, type=ModuleNotFoundError"):
            deploy.Delivery.start_guard(operator, deployment())
        self.assertEqual(operator.state['guard_failure'], {'phase': 'imports', 'error_type': 'ModuleNotFoundError'})
        self.assertNotIn('must-not-log', json.dumps(operator.state))
        operator.cluster.patch.assert_not_called()


class MaintenanceTests(unittest.TestCase):
    def run_guard(self, *, pending=0, acquired=True, fingerprints=None):
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        statements = []
        def execute(statement, parameters=None):
            sql = str(statement); statements.append((sql, parameters))
            result = Mock()
            result.scalar_one.return_value = acquired if 'pg_try_advisory_lock' in sql else pending if 'count(*)' in sql else True
            result.scalars.return_value = ['20261028_01']
            return result
        connection.execute.side_effect = execute
        with patch('sqlalchemy.create_engine', return_value=engine), \
             patch.object(maintenance, 'APP_ROOT', Path(__file__).resolve().parents[1]), \
             patch('backend.app.core.config.get_settings', return_value=SimpleNamespace(database_url='fixture')), \
             patch.object(maintenance, 'fingerprint', side_effect=fingerprints or [{'same': 'identity'}, {'same': 'identity'}]), \
             patch.object(maintenance, 'write_status') as status, \
             patch.object(maintenance, 'RELEASE') as signal, \
             patch.object(maintenance.time, 'sleep'), patch('builtins.print'):
            signal.exists.return_value = True
            outcome = maintenance.main()
        return outcome, statements, status, engine

    def test_shared_application_barrier_is_held_before_reading_activity(self):
        outcome, statements, status, engine = self.run_guard()
        self.assertEqual(outcome, 0)
        keys = [parameters['key'] for sql, parameters in statements if 'pg_try_advisory_lock' in sql]
        self.assertEqual(keys, [maintenance.LOCK_KEY + 2, maintenance.LOCK_KEY])
        self.assertTrue(all(sql.startswith('SELECT') for sql, _ in statements))
        self.assertEqual([c.kwargs['state'] for c in status.call_args_list], ['held', 'released'])
        engine.dispose.assert_called_once()

    def test_queued_training_or_unavailable_barrier_cannot_enter_maintenance(self):
        for options in ({'pending': 1}, {'acquired': False}):
            with self.subTest(options=options):
                outcome, _, status, engine = self.run_guard(**options)
                self.assertEqual(outcome, 1)
                self.assertNotIn('held', [c.kwargs['state'] for c in status.call_args_list])
                engine.dispose.assert_called_once()

    def test_changed_application_identity_is_not_reported_as_success(self):
        outcome, _, status, _ = self.run_guard(fingerprints=[{'active': 'before'}, {'active': 'after'}])
        self.assertEqual(outcome, 1)
        self.assertEqual(status.call_args.kwargs['state'], 'failed')

    def test_import_failure_is_recorded_before_engine_creation(self):
        with patch.object(maintenance, 'load_runtime', side_effect=ModuleNotFoundError('private-import-detail')), \
             patch.object(maintenance, 'write_status') as status, patch('builtins.print') as output:
            self.assertEqual(maintenance.main(), 1)
        self.assertEqual(status.call_args.kwargs, {'state': 'failed', 'phase': 'imports', 'error_type': 'ModuleNotFoundError'})
        self.assertNotIn('private-import-detail', str(output.call_args_list))

    def test_image_import_smoke_never_evaluates_settings_or_opens_database(self):
        factory, settings = Mock(), Mock()
        with patch.object(maintenance, 'load_runtime', return_value=(factory, Mock(), settings)), patch('builtins.print'):
            self.assertEqual(maintenance.check_imports(), 0)
        factory.assert_not_called()
        settings.assert_not_called()

    def test_guard_outside_app_imports_explicit_app_root_in_fresh_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = root / 'application'
            configuration = application / 'backend/app/core/config.py'
            configuration.parent.mkdir(parents=True)
            for folder in (application / 'backend', application / 'backend/app', configuration.parent):
                (folder / '__init__.py').write_text('')
            configuration.write_text("def get_settings(): return 'isolated-application'\n")
            other_cwd = root / 'unrelated-working-directory'; other_cwd.mkdir()
            script = "import json,runpy,sys; m=runpy.run_path(sys.argv[1],run_name='guard_probe'); _,_,settings=m['load_runtime'](sys.argv[2]); print(json.dumps({'settings':settings(),'source':settings.__code__.co_filename}))"
            environment = {key: value for key, value in os.environ.items() if key not in {'PYTHONPATH', 'PYTHONHOME'}}
            result = subprocess.run([sys.executable, '-B', '-c', script, str(Path(maintenance.__file__).resolve()), str(application)],
                                    cwd=other_cwd, env=environment, text=True, encoding='utf-8', capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            proof = json.loads(result.stdout)
            self.assertEqual(proof['settings'], 'isolated-application')
            self.assertEqual(Path(proof['source']).resolve(), configuration.resolve())


if __name__ == "__main__":
    unittest.main()
