import copy
import unittest
import uuid

from src.cloud_training.airflow_control import WorkerUnavailable, finalize, train


class AirflowControlTests(unittest.TestCase):
    def test_transient_status_reply_failure_keeps_same_attempt_and_does_not_stop_siblings(self):
        polls = [0]
        def send(role, path, payload=None):
            self.calls.append((role, path, payload))
            result = self.response()
            if path.endswith("/status"):
                polls[0] += 1
                if polls[0] == 1:
                    raise WorkerUnavailable()
                result.update(branch_state="ready", artifact_receipt=self.receipt("tree"))
            return result
        result = train(self.conf, "tree", send=send, sleep=lambda _: None, log=self.logs.append)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(sum(path.endswith("/start") for _, path, _ in self.calls), 1)
        self.assertFalse(any(path.endswith("/stop") for _, path, _ in self.calls))
        self.assertEqual([payload["attempt_id"] for _, path, payload in self.calls if path.endswith("/status")], [self.attempt] * 2)
    def setUp(self):
        self.conf = {name: str(uuid.uuid4()) for name in ("training_run_id", "region_id", "dataset_id")}
        self.conf["training_backend"] = "kaggle"
        self.attempt, self.calls, self.logs = str(uuid.uuid4()), [], []
    def response(self):
        return {**self.conf, "backend": "kaggle", "attempt_id": self.attempt, "state": "running", "branch_state": "running",
                "events": [], "execution_worker": {"control_revision": "cloud-training-v1"}}
    def receipt(self, branch):
        return {"version": 1, "region_id": self.conf["region_id"], "run_id": self.conf["training_run_id"],
                "branch": branch, "bundle_id": str(uuid.uuid4()), "manifest_sha256": "a" * 64}
    def test_poll_logs_progress_but_xcom_contains_only_the_receipt(self):
        def send(role, path, payload=None):
            self.calls.append((role, path, payload))
            result = self.response()
            result["events"] = [{"sequence": 1, "code": "epoch_completed", "details": {"epoch": 2, "epochs": 10}}]
            if path.endswith("/status"):
                result.update(branch_state="ready", artifact_receipt=self.receipt("tree"))
            return result
        result = train(self.conf, "tree", send=send, sleep=lambda _: None, log=self.logs.append)
        self.assertEqual(result["backend"], "kaggle")
        self.assertNotIn("events", result)
        self.assertEqual(sum("epoch_completed" in item for item in self.logs), 1)
        self.assertEqual(self.calls[1][2]["after_sequence"], 1)
    def test_failure_requests_stop_on_both_roles_without_local_fallback(self):
        def send(role, path, payload=None):
            self.calls.append((role, path, payload))
            if path.endswith("/stop"):
                return {}
            return {**self.response(), "cancel_requested": True, "state": "cancelling"}
        with self.assertRaises(RuntimeError):
            train(self.conf, "tree", send=send, sleep=lambda _: None, log=self.logs.append)
        self.assertEqual([role for role, path, _ in self.calls if path.endswith("/stop")], ["tree", "recurrent"])
        self.assertEqual(sum(path.endswith("/start") for _, path, _ in self.calls), 1)
    def test_wrong_backend_or_attempt_is_rejected(self):
        def send(role, path, payload=None):
            if path.endswith("/stop"):
                return {}
            return {**self.response(), "backend": "local"}
        with self.assertRaises(WorkerUnavailable):
            train(self.conf, "tree", send=send, sleep=lambda _: None, log=self.logs.append)
    def test_finalize_retries_a_lost_response_without_restarting_fits(self):
        receipts = {branch: self.receipt(branch) for branch in ("tree", "lstm", "gru")}
        def send(role, path, payload=None):
            self.calls.append(path)
            if len(self.calls) == 1:
                raise WorkerUnavailable()
            return {"training_run_status": "completed"}
        result = finalize(self.conf, receipts, send=send, sleep=lambda _: None)
        self.assertEqual(result["training_run_status"], "completed")
        self.assertTrue(all(path.endswith("/finalize") for path in self.calls))


if __name__ == "__main__":
    unittest.main()
