"""Offline execution/admission tests. No cluster, Kaggle or ML fitting."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from backend.app.core.config import Settings
from src.cloud_training.artifacts import ArtifactError, write_bundle
from src.cloud_training.kaggle_execution import TrainingCancelled
from src.cloud_training.package import configuration_digest, prepare_package
from src.local_training.execution import LocalExecution, initial_ledger
from src.local_training.kubernetes import Kubernetes, MODELS, manifest

ROOT = Path(__file__).resolve().parents[1]
NAMES = dict(zip(MODELS, ("RandomForest", "XGBoost", "LightGBM", "LSTM", "GRU")))


class MemoryS3:
    def __init__(self):
        self.objects = {}

    def upload_file(self, filename, bucket, key):
        self.objects[key] = Path(filename).read_bytes()

    def put_object(self, *, Key, Body, **kwargs):
        self.objects[Key] = Body

    def get_object(self, *, Key, **kwargs):
        raw = self.objects[Key]
        return {"Body": io.BytesIO(raw), "ContentLength": len(raw)}


class FakeKube:
    def __init__(self, store, context, root):
        self.store, self.context, self.root = store, context, root
        self.jobs, self.created = {}, []
        self.capacity_checks = 0
        self.delay_capacity = 0
        self.corrupt = False
        self.scores = {}

    def available(self, *args):
        self.capacity_checks += 1
        return [] if self.capacity_checks <= self.delay_capacity else ["mlops-node-02", "mlops-node-03"]

    def get(self, name):
        return self.jobs.get(name)

    def create(self, desired):
        name = desired["metadata"]["name"]
        self.created.append(copy.deepcopy(desired))
        job = copy.deepcopy(desired)
        job["metadata"]["uid"] = str(uuid.uuid4())
        job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        self.jobs[name] = job
        environment = {e["name"]: e.get("value") for e in desired["spec"]["template"]["spec"]["containers"][0]["env"]}
        model = environment["LOCAL_MODEL"]
        output = self.root / name
        output.mkdir()
        weights, report, sample = output / "model.bin", output / "shap.json", output / "sample.json"
        weights.write_bytes(b"synthetic-model-never-deserialized")
        report.write_text('{"status":"completed"}')
        sample.write_text("{}")
        score = self.scores.get(model, 5 - MODELS.index(model))
        candidate = {"best_model_name": NAMES[model], "validation_MAE": round(score, 4), "selection_cv_mae": score,
            "model_file": str(weights), "compatibility_file": str(sample),
            "model_family": "legacy_sklearn" if model.endswith("_lag") else "neural_sequence",
            "shap_reports": {NAMES[model]: {"source_report": str(report)}},
            "model_comparison": [{"model_name": NAMES[model]}]}
        result = {"branch": self.context["branch"], "status": "completed", "candidates": [candidate], "production_candidate": candidate}
        bundle = write_bundle(result, self.context, output)
        prefix = environment["LOCAL_INPUT_PREFIX"] + "output/" + name + "/"
        for item in bundle["files"]:
            self.store.objects[prefix + item["path"]] = (output / item["path"]).read_bytes()
        self.store.objects[prefix + "manifest.json"] = (output / "manifest.json").read_bytes()
        if self.corrupt:
            self.store.objects[prefix + "model.bin"] = b"X" * weights.stat().st_size
        return job

    def pods(self, name):
        return []

    def stop(self, name, uid):
        current = self.jobs.pop(name, None)
        if current and current["metadata"]["uid"] != uid:
            raise AssertionError("Ownership changed")
        return True


class LocalExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(_env_file=None, training_control_enabled=True, local_training_enabled=True,
                                 local_training_image="ghcr.io/example/training@sha256:" + "a" * 64)
        self.store = SimpleNamespace(client=MemoryS3(), bucket="synthetic", prefix="synthetic")
        self.created = []

    def provider(self, branch, selected=None):
        config = {"training_backend": "local", "selected_models": list(selected or MODELS),
                  "train_start_date": "2026-01-01", "train_end_date": "2026-11-01"}
        context = {k: str(uuid.uuid4()) for k in ("run_id", "region_id", "dataset_id", "attempt_id")}
        context.update(branch=branch, configuration_sha256=configuration_digest(config))
        directory = self.root / branch
        directory.mkdir()
        data = self.root / "data.csv"
        data.write_bytes(b"date_time,traffic_volume\n2026-01-01,1\n")
        snapshot = prepare_package(ROOT, data, config, context, directory / "input", hashlib.sha256(data.read_bytes()).hexdigest())
        kube = FakeKube(self.store.client, context, self.root)
        ledger = initial_ledger(snapshot)
        elapsed = [0]
        def sleep(seconds):
            elapsed[0] += seconds
        provider = LocalExecution(self.store, directory, ledger, lambda value: None, self.settings,
                                  client=kube, sleep=sleep, clock=lambda: elapsed[0])
        return provider, kube, elapsed

    def test_all_five_models_run_in_individual_jobs_and_tree_reports_are_combined(self):
        submitted, winners = [], []
        for branch in ("tree", "lstm", "gru"):
            provider, kube, _ = self.provider(branch)
            result = provider.run(lambda event: None, lambda: False)
            submitted.extend(next(e["value"] for e in j["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "LOCAL_MODEL") for j in kube.created)
            winners.extend(c["best_model_name"] for c in result["candidates"])
            self.assertTrue(provider.ledger["remote_stopped"])
            self.assertFalse(kube.jobs)
            if branch == "tree":
                self.assertEqual(set(result["production_candidate"]["shap_reports"]), {"RandomForest", "XGBoost", "LightGBM"})
                self.assertEqual(len(result["production_candidate"]["model_comparison"]), 3)
        self.assertEqual(submitted, list(MODELS))
        self.assertEqual(winners, ["LightGBM", "LSTM", "GRU"])

    def test_resource_queue_does_not_create_jobs_until_admitted(self):
        provider, kube, elapsed = self.provider("gru", ["gru"])
        kube.delay_capacity = 2
        provider.run(lambda event: None, lambda: False)
        self.assertEqual(len(kube.created), 1)
        self.assertEqual(kube.capacity_checks, 3)
        self.assertGreaterEqual(elapsed[0], 6)

    def test_local_tree_winner_uses_unrounded_cv_score_like_cloud_pipeline(self):
        provider, kube, _ = self.provider("tree")
        kube.scores = {"random_forest_lag": 1.00004, "xgboost_lag": 1.00003, "lightgbm_lag": 2}
        result = provider.run(lambda event: None, lambda: False)
        self.assertEqual(result["production_candidate"]["best_model_name"], "XGBoost")

    def test_cancel_while_waiting_never_submits_a_job(self):
        provider, kube, elapsed = self.provider("lstm", ["lstm"])
        kube.delay_capacity = 100
        with self.assertRaises(TrainingCancelled):
            provider.run(lambda event: None, lambda: elapsed[0] >= 3)
        self.assertFalse(kube.created)
        self.assertTrue(provider.stop())

    def test_cancel_during_capacity_check_does_not_launch_the_prepared_job(self):
        provider, kube, _ = self.provider("lstm", ["lstm"])
        stop = [False]
        def capacity(*args):
            stop[0] = True
            return ["mlops-node-02"]
        kube.available = capacity
        with self.assertRaises(TrainingCancelled):
            provider.run(lambda event: None, lambda: stop[0])
        self.assertFalse(kube.created)

    def test_corrupt_output_cannot_become_a_candidate(self):
        provider, kube, _ = self.provider("gru", ["gru"])
        kube.corrupt = True
        with self.assertRaises(ArtifactError):
            provider.run(lambda event: None, lambda: False)
        self.assertFalse(provider.ledger["remote_stopped"])

    def test_existing_job_is_reconciled_instead_of_submitted_twice(self):
        provider, kube, _ = self.provider("gru", ["gru"])
        desired = manifest(self.settings, provider.ledger["snapshot"]["context"], "gru", provider.prefix, self.store.bucket)
        kube.create(desired)
        result = provider.run(lambda event: None, lambda: False)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(kube.created), 1)

    def test_job_owner_or_image_mismatch_is_rejected(self):
        provider, kube, _ = self.provider("gru", ["gru"])
        desired = manifest(self.settings, provider.ledger["snapshot"]["context"], "gru", provider.prefix, self.store.bucket)
        current = kube.create(desired)
        current["metadata"]["annotations"]["traffic.mlops/identity"] = "different"
        with self.assertRaisesRegex(RuntimeError, "identity"):
            provider.run(lambda event: None, lambda: False)

    def test_every_model_has_same_node_isolation_limits_and_pinned_image(self):
        provider, _, _ = self.provider("tree")
        for model in MODELS:
            spec = manifest(self.settings, provider.ledger["snapshot"]["context"], model, provider.prefix, self.store.bucket)["spec"]
            pod = spec["template"]["spec"]
            self.assertEqual(spec["backoffLimit"], 0)
            self.assertFalse(pod["automountServiceAccountToken"])
            environment = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
            self.assertEqual(environment["S3_ENDPOINT_URL"], self.settings.s3_endpoint_url)
            self.assertEqual(pod["containers"][0]["resources"]["requests"]["memory"], "3072Mi")
            self.assertEqual(pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]["topologyKey"], "kubernetes.io/hostname")
            self.assertEqual(pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"], ["mlops-node-02", "mlops-node-03"])

    def test_capacity_excludes_control_plane_stale_metrics_and_pending_reservations(self):
        kube = object.__new__(Kubernetes)
        kube.namespace = "traffic-mlops"
        now = datetime.now(UTC)
        nodes = [{"metadata": {"name": f"mlops-node-0{n}"}, "spec": {}, "status": {
            "allocatable": {"memory": "8Gi", "cpu": "4"}, "conditions": [
                {"type": "Ready", "status": "True"}, {"type": "MemoryPressure", "status": "False"},
                {"type": "DiskPressure", "status": "False"}]}} for n in (1, 2, 3)]
        metrics = [{"metadata": node["metadata"], "timestamp": now.isoformat(),
                    "usage": {"memory": "2Gi", "cpu": "500000000n"}} for node in nodes]
        reservations = []
        def call(method, path, **kwargs):
            if "metrics.k8s.io" in path:
                return {"items": metrics}
            if path == "/api/v1/nodes":
                return {"items": nodes}
            return {"items": reservations if "/jobs" in path else []}
        kube.call = call
        self.assertEqual(kube.available(3072, 1536), ["mlops-node-02", "mlops-node-03"])
        metrics[2]["timestamp"] = (now - timedelta(minutes=5)).isoformat()
        self.assertEqual(kube.available(3072, 1536), ["mlops-node-02"])
        reservations.append({"spec": {"template": {"spec": {"affinity": {"nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{
                "key": "kubernetes.io/hostname", "operator": "In", "values": ["mlops-node-02"]}]}]}}}}}}})
        self.assertEqual(kube.available(3072, 1536), [])
        reservations.clear()
        metrics[1]["usage"]["cpu"] = "3500m"
        self.assertEqual(kube.available(3072, 1536), [])
        metrics[1]["usage"] = {"memory": "5Gi", "cpu": "500m"}
        self.assertEqual(kube.available(3072, 1536), [])


if __name__ == "__main__":
    unittest.main()
