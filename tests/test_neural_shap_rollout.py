import copy
import unittest

from scripts.shap.rollout_neural_shap import neural_patch


class NeuralShapRolloutTests(unittest.TestCase):
    def setUp(self):
        self.tree = {"spec": {"template": {"spec": {
            "containers": [{"name": "training-worker", "image": "image@sha256:pinned", "volumeMounts": [
                {"name": "shap-runtime", "mountPath": "/opt/traffic-shap", "readOnly": True},
                {"name": "shap-dependencies", "mountPath": "/opt/traffic-shap-deps", "readOnly": True}]}],
            "volumes": [{"name": "shap-runtime", "configMap": {"name": "immutable-shap"}},
                        {"name": "shap-dependencies", "emptyDir": {"sizeLimit": "512Mi"}}],
            "initContainers": [{"name": "install-shap", "image": "image@sha256:pinned",
                                "env": [{"name": "MINIO_ROOT_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "existing", "key": "password"}}}]}]}}}}
        self.neural = {"spec": {"template": {"spec": {"volumes": [{"name": "existing"}],
            "containers": [{"name": "training-worker", "image": "image@sha256:pinned", "volumeMounts": []}]}}}}

    def test_narrow_patch_preserves_command_image_backend_and_existing_mounts(self):
        before = copy.deepcopy((self.tree, self.neural))
        result = neural_patch(self.tree, self.neural, {"metadata": {"name": "new-code"}}, [{"key": "a", "path": "b"}])
        pod = result["spec"]["template"]["spec"]
        self.assertEqual(set(pod), {"volumes", "initContainers", "containers"})
        self.assertEqual(set(pod["containers"][0]), {"name", "volumeMounts", "env"})
        self.assertEqual(pod["initContainers"], self.tree["spec"]["template"]["spec"]["initContainers"])
        self.assertEqual((self.tree, self.neural), before)

    def test_rejects_existing_neural_shap_instead_of_overwriting_it(self):
        self.neural["spec"]["template"]["spec"]["volumes"].append({"name": "shap-dependencies"})
        with self.assertRaises(RuntimeError): neural_patch(self.tree, self.neural, {}, [])

    def test_rejects_literal_credential_export_to_review(self):
        self.tree["spec"]["template"]["spec"]["initContainers"][0]["env"] = [{"name": "INTERNAL_TOKEN", "value": "synthetic-secret"}]
        with self.assertRaises(RuntimeError): neural_patch(self.tree, self.neural, {}, [])

    def test_rejects_different_worker_image(self):
        self.neural["spec"]["template"]["spec"]["containers"][0]["image"] = "different"
        with self.assertRaises(RuntimeError): neural_patch(self.tree, self.neural, {}, [])


if __name__ == "__main__":
    unittest.main()
