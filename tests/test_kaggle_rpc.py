import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from src.cloud_training.kaggle_rpc import api_for, install_http_limits
from src.cloud_training.kaggle_transport import ProviderError, SDKTransport


class KaggleRPCTests(unittest.TestCase):
    def test_status_read_retries_a_transient_rpc_failure_without_submitting_a_fit(self):
        failure = subprocess.CompletedProcess([], 2, json.dumps({"ok": False, "error_type": "ConnectTimeout"}))
        success = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "result": {"state": "RUNNING"}}))
        with patch("src.cloud_training.kaggle_transport.subprocess.run", side_effect=[failure, success]) as run, \
             patch("src.cloud_training.kaggle_transport.time.sleep"):
            result = SDKTransport("probeuser").call("status", reference="probeuser/notebook/1")
        self.assertEqual(result["state"], "RUNNING")
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(json.loads(call.kwargs["input"])["operation"], "status")

    def test_ambiguous_kernel_write_is_never_repeated_by_rpc_retry(self):
        failure = subprocess.CompletedProcess([], 2, json.dumps({"ok": False, "error_type": "ReadTimeout"}))
        with patch("src.cloud_training.kaggle_transport.subprocess.run", return_value=failure) as run:
            with self.assertRaises(ProviderError):
                SDKTransport("probeuser").call("kernel_push", directory="synthetic", no_run=False)
        self.assertEqual(run.call_count, 1)

    def test_read_retries_share_one_deadline(self):
        clock = [100.0]
        def timeout(*args, **kwargs):
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        with patch("src.cloud_training.kaggle_transport.time.monotonic", side_effect=lambda: clock[0]), \
             patch("src.cloud_training.kaggle_transport.subprocess.run", side_effect=timeout) as run:
            with self.assertRaises(ProviderError):
                SDKTransport("probeuser").call("status", timeout=10, reference="probeuser/notebook/1")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(clock[0], 110)

    def test_sdk_timeout_none_is_bounded_before_network_call(self):
        original = Mock(return_value="response")
        with patch.object(requests.Session, "send", original):
            install_http_limits()
            result = requests.Session.send("session", "request", timeout=None)
        self.assertEqual(result, "response")
        self.assertEqual(original.call_args.kwargs["timeout"], (5, 30))

    def test_undelivered_connect_timeout_retries_with_a_bound(self):
        original = Mock(side_effect=[requests.exceptions.ConnectTimeout(), requests.exceptions.ConnectTimeout(), "response"])
        with patch.object(requests.Session, "send", original), patch("src.cloud_training.kaggle_rpc.time.sleep"):
            install_http_limits()
            self.assertEqual(requests.Session.send("session", "request"), "response")
        self.assertEqual(original.call_count, 3)

    def test_connect_retry_budget_is_not_unbounded(self):
        original = Mock(side_effect=requests.exceptions.ConnectTimeout())
        with patch.object(requests.Session, "send", original), patch("src.cloud_training.kaggle_rpc.time.sleep"):
            install_http_limits()
            with self.assertRaises(requests.exceptions.ConnectTimeout):
                requests.Session.send("session", "request")
        self.assertEqual(original.call_count, 3)

    def test_ambiguous_response_timeout_never_repeats_a_write(self):
        original = Mock(side_effect=requests.exceptions.ReadTimeout())
        with patch.object(requests.Session, "send", original):
            install_http_limits()
            with self.assertRaises(requests.exceptions.ReadTimeout):
                requests.Session.send("session", "write-request", timeout=None)
        self.assertEqual(original.call_count, 1)

    def test_auth_reuses_the_import_authenticated_api(self):
        api = SimpleNamespace(config_values={"username": "probeuser"}, CONFIG_NAME_USER="username")
        with patch.dict("sys.modules", {"kaggle": SimpleNamespace(api=api)}), \
             patch.dict("os.environ", {"KAGGLE_TOKEN_FILE": ""}), \
             patch("src.cloud_training.kaggle_rpc.install_http_limits") as limits:
            self.assertIs(api_for("probeuser"), api)
        limits.assert_called_once()

    def test_transport_preserves_normalized_failure_but_never_raw_provider_text(self):
        response = {"ok": False, "error_type": "ConnectTimeout", "http_status": None, "raw": "PRIVATE_PROVIDER_TEXT"}
        with patch("src.cloud_training.kaggle_transport.subprocess.run", return_value=subprocess.CompletedProcess([], 2, json.dumps(response))):
            with self.assertRaises(ProviderError) as caught:
                SDKTransport("probeuser").call("dataset_create", directory="synthetic")
        self.assertEqual(caught.exception.operation, "dataset_create")
        self.assertEqual(caught.exception.category, "connect_timeout")
        self.assertNotIn("PRIVATE_PROVIDER_TEXT", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
