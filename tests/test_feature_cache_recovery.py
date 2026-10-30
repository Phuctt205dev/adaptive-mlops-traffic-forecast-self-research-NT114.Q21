import copy
import unittest
from unittest.mock import Mock

from scripts.shap.recover_feature_importance_cache import CacheRecovery, GIB, check_capacity, storage_patch


class FeatureCacheRecoveryTests(unittest.TestCase):
    def spec(self):
        return {"replicas": 2, "template": {"spec": {
            "volumes": [{"name": "artifact-cache", "emptyDir": {"sizeLimit": "2Gi"}},
                        {"name": "cloud-code", "configMap": {"name": "reviewed-source"}}],
            "containers": [{"name": "traffic-api", "image": "unchanged", "command": ["unchanged"],
                "resources": {"limits": {"memory": "3Gi", "cpu": "1", "ephemeral-storage": "2Gi"},
                              "requests": {"memory": "1536Mi", "cpu": "250m", "ephemeral-storage": "512Mi"}}}],
            "initContainers": [{"name": "restore-artifact-cache", "image": "unchanged", "command": ["unchanged"],
                "resources": {"limits": {"memory": "1Gi", "cpu": "500m", "ephemeral-storage": "1Gi"},
                              "requests": {"memory": "256Mi", "cpu": "100m", "ephemeral-storage": "512Mi"}}}],
        }}}

    def test_patch_increases_disk_budget_and_requests_without_changing_memory_or_sources(self):
        spec = self.spec()
        original = copy.deepcopy(spec)
        pod = storage_patch(spec)["spec"]["template"]["spec"]
        self.assertEqual(spec, original)
        self.assertEqual(pod["volumes"], [{"name": "artifact-cache", "emptyDir": {"sizeLimit": "4Gi"}}])
        self.assertEqual(pod["containers"][0]["resources"]["limits"]["memory"], "3Gi")
        self.assertEqual(pod["containers"][0]["resources"]["limits"]["ephemeral-storage"], "4Gi")
        self.assertEqual(pod["initContainers"][0]["resources"]["requests"]["ephemeral-storage"], "3Gi")
        self.assertNotIn("command", pod["containers"][0])
        self.assertNotIn("image", pod["initContainers"][0])

    def test_rejects_unreviewed_cache_baseline(self):
        spec = self.spec()
        spec["template"]["spec"]["volumes"][0]["emptyDir"]["sizeLimit"] = "8Gi"
        with self.assertRaisesRegex(RuntimeError, "baseline changed"):
            storage_patch(spec)

    def test_current_restore_fits_four_gib_but_full_target_or_tight_node_blocks(self):
        nodes = {name: {"availableBytes": 20 * GIB} for name in ("mlops-node-02", "mlops-node-03")}
        check_capacity({"restore_bytes": 2386234319}, nodes)
        for amount in (4 * GIB, -1, int(3.5 * GIB)):
            with self.subTest(bytes=amount), self.assertRaises(RuntimeError):
                check_capacity({"restore_bytes": amount}, nodes)
        nodes["mlops-node-02"]["availableBytes"] = 5 * GIB
        with self.assertRaisesRegex(RuntimeError, "free disk"):
            check_capacity({"restore_bytes": 2386234319}, nodes)

    def test_apply_cannot_replay_and_resume_requires_started_incomplete_recovery(self):
        operator = CacheRecovery.__new__(CacheRecovery)
        operator.journal = {"apply_started": True}
        operator.cluster = Mock()
        with self.assertRaisesRegex(RuntimeError, "already started"):
            operator.apply()
        operator.cluster.patch.assert_not_called()
        for journal in ({}, {"apply_started": True, "applied": True}):
            operator.journal = journal
            with self.assertRaisesRegex(RuntimeError, "Resume requires"):
                operator.resume()

    def test_only_verified_recovery_restores_original_dag_pause_state(self):
        operator = CacheRecovery.__new__(CacheRecovery)
        operator.review = {"dag_was_paused": False}
        operator.cluster = Mock()
        operator.save = Mock()
        operator.verify = Mock(side_effect=RuntimeError("not ready"))
        with self.assertRaises(RuntimeError):
            operator.complete()
        operator.cluster.pause.assert_not_called()
        operator.verify = Mock(return_value={"cache_recovery_verified": True})
        self.assertTrue(operator.complete()["dag_state_restored"])
        operator.cluster.pause.assert_called_once_with(False)
        operator.save.assert_called_with(applied=True, dag_state_restored=True)
