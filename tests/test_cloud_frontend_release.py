import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.cloud_training.prepare_frontend import PUBLISH_FILES, validate_targets
from scripts.cloud_training.publish_frontend import publish


class FrontendReleaseTests(unittest.TestCase):
    def test_public_targets_reject_credentials_queries_and_unsafe_base_paths(self):
        for url in ("http://example.com/api/v1", "https://token@example.com/api/v1", "https://example.com/api/v1?key=secret"):
            with self.assertRaises(ValueError):
                validate_targets(url, "/project/")
        for path in ("project/", "/../", "/x?token/"):
            with self.assertRaises(ValueError):
                validate_targets("https://example.com/api/v1", path)

    def fixture(self, directory):
        root = Path(directory)
        (root / "review.json").write_text(json.dumps({"bundle_id": "bundle"}))
        (root / "frontend-dist.zip").write_bytes(b"reviewed archive")
        data = {"build_passed": True, "bundle_id": "bundle", "publish_files": PUBLISH_FILES,
                "api_base_url": "https://example.com/api/v1", "base_path": "/project/", "source_files": {"source": "hash"},
                "git_head": "head", "branch": "master", "origin_sha256": hashlib.sha256(b"origin").hexdigest(),
                "dist_sha256": hashlib.sha256(b"reviewed archive").hexdigest()}
        (root / "frontend-build.json").write_text(json.dumps(data))
        return root

    @staticmethod
    def git(*args):
        return {("branch", "--show-current"): "master", ("rev-parse", "HEAD"): "head",
                ("remote", "get-url", "origin"): "origin", ("diff", "--cached", "--name-only"): ""}[args]

    def test_preview_checks_source_and_git_without_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            with patch("scripts.cloud_training.publish_frontend.source_hashes", return_value={"source": "hash"}), \
                 patch("scripts.cloud_training.publish_frontend.git", side_effect=self.git), \
                 patch("scripts.cloud_training.publish_frontend.checked_git", side_effect=AssertionError("No Git writes")):
                result = publish(root)
                self.assertTrue(result["frontend_preview_verified"])
                self.assertFalse(result["published"])

    def test_changed_source_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            with patch("scripts.cloud_training.publish_frontend.source_hashes", return_value={"source": "changed"}), \
                 patch("scripts.cloud_training.publish_frontend.checked_git", side_effect=AssertionError("No Git writes")):
                with self.assertRaisesRegex(RuntimeError, "source/archive differs"):
                    publish(root, do_publish=True)

    def test_publish_requires_live_backend_verification_arguments_before_git_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            with patch("scripts.cloud_training.publish_frontend.source_hashes", return_value={"source": "hash"}), \
                 patch("scripts.cloud_training.publish_frontend.git", side_effect=self.git), \
                 patch("scripts.cloud_training.publish_frontend.checked_git", side_effect=AssertionError("No Git writes")):
                with self.assertRaisesRegex(RuntimeError, "requires the reviewed kubectl/snapshot"):
                    publish(root, do_publish=True)


if __name__ == "__main__":
    unittest.main()
