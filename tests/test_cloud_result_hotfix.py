from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.cloud_training.result_hotfix import ResultFix
from pathlib import Path


class CloudResultHotfixTests(unittest.TestCase):
    def fixture(self):
        fix = object.__new__(ResultFix)
        fix.journal = {}
        fix.directory = Path("review")
        fix.check = Mock(return_value={"dag_paused": False})
        fix.save = Mock()
        fix.before = SimpleNamespace(pause=Mock())
        fix.after = SimpleNamespace(call=Mock(), patch=Mock(), pause=Mock())
        fix.idle = Mock()
        fix.verify = Mock()
        return fix

    def test_new_work_after_pause_prevents_any_rollout(self):
        fix = self.fixture()
        fix.idle.side_effect = RuntimeError("New work is active")
        with self.assertRaises(RuntimeError):
            fix.apply()
        fix.after.call.assert_not_called()
        fix.after.patch.assert_not_called()
        fix.before.pause.assert_called_once_with(True)
        fix.save.assert_called_once_with(apply_started=True, dag_was_paused=False)

    def test_failed_patch_keeps_dag_paused_and_never_claims_success(self):
        fix = self.fixture()
        fix.after.patch.side_effect = RuntimeError("Rollout failed")
        with patch("scripts.cloud_training.result_hotfix.read", return_value={}):
            with self.assertRaises(RuntimeError):
                fix.apply()
        fix.verify.assert_not_called()
        fix.after.pause.assert_not_called()
        fix.before.pause.assert_called_once_with(True)
        fix.save.assert_called_once_with(apply_started=True, dag_was_paused=False)


if __name__ == "__main__":
    unittest.main()
