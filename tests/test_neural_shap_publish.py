import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.shap import publish_neural_shap as release


class NeuralShapPublishTests(unittest.TestCase):
    def test_only_reviewed_source_paths_can_be_published(self):
        with patch.object(release, "git", side_effect=["src/shap_reporting.py", "scripts/shap/check_neural_runtime.py"]):
            self.assertEqual(release.changed_files(), ["scripts/shap/check_neural_runtime.py", "src/shap_reporting.py"])
        for path in ("other-work.py", "data/private.csv", "Deployment_test_on_rke2/runtime/cloud_training_runtime.py"):
            with self.subTest(path=path), patch.object(release, "git", side_effect=[path, ""]):
                with self.assertRaises(RuntimeError): release.changed_files()

    def preview(self, *, changed=False, staged=False):
        review = {"build_passed": True, "bundle_id": "reviewed", "sources": {"source": "sha"},
                  "publish_files": ["src/shap_reporting.py"], "dist_sha256": "archive", "branch": "master",
                  "git_head": "head", "origin_sha256": hashlib.sha256(b"origin").hexdigest(),
                  "api_base_url": "https://example.test/api/v1", "base_path": "/project/"}
        calls = []
        def git(*args):
            calls.append(args)
            if args == ("branch", "--show-current"): return "master"
            if args == ("rev-parse", "HEAD"): return "head"
            if args == ("remote", "get-url", "origin"): return "origin"
            if args == ("diff", "--cached", "--name-only"): return "other.py" if staged else ""
            raise AssertionError("Preview must not stage/commit/push or query the cluster")
        with patch.object(release, "read", return_value=review), patch.object(release, "hashes", return_value={} if changed else review["sources"]), \
                patch.object(release, "sha", return_value="archive"), patch.object(release, "changed_files", return_value=review["publish_files"]), \
                patch.object(release, "git", side_effect=git), patch.object(release, "NeuralRollout", side_effect=AssertionError("Preview is local only")):
            result = release.publish(Path("synthetic-review"))
        return result, calls

    def test_preview_is_read_only_and_does_not_require_applied_backend(self):
        result, calls = self.preview()
        self.assertTrue(result["neural_frontend_preview_verified"])
        self.assertFalse(result["git_changed"])
        self.assertFalse(any(call[0] in {"add", "commit", "push"} for call in calls))

    def test_changed_source_or_existing_staged_work_stops_publish(self):
        for values in ({"changed": True}, {"staged": True}):
            with self.subTest(values=values), self.assertRaises(RuntimeError): self.preview(**values)


if __name__ == "__main__":
    unittest.main()
