from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.cloud_training.network_hotfix import Hotfix


class CloudNetworkHotfixTests(unittest.TestCase):
    def fixture(self):
        fix = object.__new__(Hotfix)
        fix.journal = {}
        fix.cluster = SimpleNamespace(pause=Mock(), call=Mock(), patch=Mock())
        fix.save = Mock()
        fix.idle = Mock()
        fix.verify = Mock()
        return fix

    def test_failed_precheck_never_starts_cluster_changes(self):
        fix = self.fixture()
        fix.check = Mock(side_effect=RuntimeError("Proof incomplete"))
        with self.assertRaises(RuntimeError):
            fix.apply()
        fix.cluster.pause.assert_not_called()
        fix.cluster.call.assert_not_called()
        fix.cluster.patch.assert_not_called()

    def test_partial_rollout_failure_keeps_dag_paused_and_journal(self):
        fix = self.fixture()
        fix.check = Mock(return_value={"dag_paused": False})
        fix.cluster.patch.side_effect = RuntimeError("Rollout failed")
        with patch("scripts.cloud_training.network_hotfix.read", return_value={}):
            with self.assertRaises(RuntimeError):
                fix.apply()
        fix.cluster.pause.assert_called_once_with(True)
        fix.save.assert_called_once_with(apply_started=True, dag_was_paused=False)
        fix.verify.assert_not_called()

    def test_uncertain_apply_is_not_blindly_replayed(self):
        fix = self.fixture()
        fix.journal["apply_started"] = True
        fix.check = Mock()
        with self.assertRaisesRegex(RuntimeError, "already started"):
            fix.apply()
        fix.check.assert_not_called()
        fix.cluster.patch.assert_not_called()

    def test_verify_uses_prepared_fix_identity_without_changing_baseline_review(self):
        fix = object.__new__(Hotfix)
        fix.review = {"bundle_id": "fix-bundle"}
        fix.cluster = SimpleNamespace(review={"bundle_id": "original-bundle"}, verify=Mock(return_value={"rollout_verified": True}))
        with tempfile.TemporaryDirectory() as directory, patch("scripts.cloud_training.network_hotfix.DIRECTORY", Path(directory)):
            result = fix.verify()
            self.assertTrue((Path(directory) / "verify-proof.json").exists())
        fix.cluster.verify.assert_called_once_with(expected=True, bundle_id="fix-bundle")
        self.assertEqual(fix.cluster.review["bundle_id"], "original-bundle")
        self.assertTrue(result["rollout_verified"])


if __name__ == "__main__":
    unittest.main()
