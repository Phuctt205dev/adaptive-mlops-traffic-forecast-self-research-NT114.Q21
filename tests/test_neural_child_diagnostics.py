import ast
import copy
import io
import json
from pathlib import Path
import queue
import re
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import requests

from src.cloud_training import notebook
from src.cloud_training.errors import FAILURE_PREFIX, safe_failure, validate_failure
from src.cloud_training.kaggle_rpc import operation
from src.cloud_training.kaggle_execution import KaggleExecution
from src.cloud_training.kaggle_transport import ProviderError
from tests.test_kaggle_execution import FakeStream


class ChildDiagnosticTests(unittest.TestCase):
    def nested_function(self, name, env):
        tree = ast.parse(Path(notebook.__file__).read_text(encoding="utf-8"))
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)
        exec(compile(ast.Module(body=[function], type_ignores=[]), "synthetic_helper.py", "exec"), env)
        return env[name]

    def test_supervisor_control_reads_retry_only_transient_errors(self):
        getter = Mock(side_effect=[requests.exceptions.ReadTimeout(), requests.exceptions.ConnectionError(), "verified-source"])
        client = SimpleNamespace(kernels=SimpleNamespace(kernels_api_client=SimpleNamespace(get_kernel=getter)))
        sleep = Mock()
        env = {"api": SimpleNamespace(build_kaggle_client=lambda: nullcontext(client)),
               "ApiGetKernelRequest": SimpleNamespace, "owner": "synthetic", "slug": "probe",
               "requests": requests, "time": SimpleNamespace(sleep=sleep)}
        read = self.nested_function("read_control_kernel", env)
        self.assertEqual(read(), "verified-source")
        self.assertEqual(getter.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        getter.reset_mock(side_effect=True)
        getter.side_effect = ValueError("invalid-source")
        with self.assertRaises(ValueError):
            read()
        self.assertEqual(getter.call_count, 1)

    def test_supervisor_http_timeout_is_bounded_even_when_sdk_sets_one(self):
        send = Mock(return_value="response")
        bounded = self.nested_function("bounded_send", {"original_send": send})
        for timeout in (None, 600, (90, 600)):
            self.assertEqual(bounded(None, None, timeout=timeout), "response")
            self.assertEqual(send.call_args.kwargs["timeout"], (5, 10))

    def test_supervisor_diagnostic_and_exit_code_survive_rpc_and_provider(self):
        context = {"run_id": "synthetic"}
        event = {"context": context, "stage": "failed", "supervisor_failure": self.failure(),
                 "supervisor_http_status": None, "supervisor_phase": "child_monitoring", "supervisor_child_returncode": -9}
        api = SimpleNamespace(kernels_logs_stream=lambda ref: [{"data": "TRAFFIC_CONTROL_EVENT " + json.dumps(event)}])
        output = io.StringIO()
        operation(api, "events", {"reference": "private/probe/1", "context": context}, output)
        packet = json.loads(output.getvalue().removeprefix("TRAFFIC_EXECUTION_EVENT "))["event"]
        provider = KaggleExecution.__new__(KaggleExecution)
        provider.stream, provider.ledger, provider.last_sequence = FakeStream([packet]), {}, 0
        provider.save = lambda **values: provider.ledger.update(copy.deepcopy(values))
        provider.drain(Mock())
        self.assertEqual(provider.ledger["supervisor_child_returncode"], -9)
        self.assertEqual(provider.ledger["supervisor_failure"], self.failure())

    def failure(self):
        return {"error_type": "ValueError", "source": "time_series_cross_validation.py", "line": 485,
                "final_source": "time_series_cross_validation.py", "final_line": 485}

    def test_exception_message_and_paths_never_enter_diagnostic(self):
        try:
            raise ValueError("PRIVATE_TOKEN=secret-private-dataset-value")
        except ValueError as error:
            failure = safe_failure(error)
        self.assertEqual(failure["error_type"], "ValueError")
        encoded = json.dumps(failure)
        self.assertNotIn("PRIVATE_TOKEN", encoded)
        self.assertNotIn("secret-private", encoded)
        self.assertNotIn("/", failure["source"])
        self.assertNotIn("\\", failure["source"])

    def test_validator_rejects_raw_text_paths_and_invalid_lines(self):
        for invalid in ({**self.failure(), "message": "secret"},
                        {**self.failure(), "source": "/private/path.py"},
                        {**self.failure(), "line": True}, {**self.failure(), "line": -1}):
            with self.assertRaises(ValueError):
                validate_failure(invalid)

    def reader(self, line):
        tree = ast.parse(Path(notebook.__file__).read_text(encoding="utf-8"))
        reader = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "read_output")
        env = {"child": SimpleNamespace(stdout=io.StringIO(line)), "json": json, "re": re,
               "CONFIG": {"context": {"run_id": "synthetic"}}, "queue_events": queue.Queue(),
               "reader_failed": SimpleNamespace(set=Mock())}
        exec(compile(ast.Module(body=[reader], type_ignores=[]), "synthetic_reader.py", "exec"), env)
        env["read_output"]()
        return env

    def test_supervisor_forwards_only_safe_run_scoped_failure(self):
        line = FAILURE_PREFIX + json.dumps({"context": {"run_id": "synthetic"}, "failure": self.failure()})
        env = self.reader("PRIVATE_TOKEN=never-forward\n" + line + "\n")
        forwarded = env["queue_events"].get_nowait()
        self.assertNotIn("PRIVATE_TOKEN", forwarded)
        payload = json.loads(forwarded.removeprefix("TRAFFIC_CONTROL_EVENT "))
        self.assertEqual(payload["failure"], self.failure())
        env["reader_failed"].set.assert_not_called()

    def test_supervisor_rejects_foreign_context_and_raw_failure_fields(self):
        for context, failure in (({"run_id": "foreign"}, self.failure()),
                                 ({"run_id": "synthetic"}, {**self.failure(), "message": "secret"})):
            env = self.reader(FAILURE_PREFIX + json.dumps({"context": context, "failure": failure}))
            env["reader_failed"].set.assert_called_once()
            self.assertTrue(env["queue_events"].empty())

    def test_rpc_preserves_failure_metadata_without_stdout_noise(self):
        context = {"run_id": "synthetic"}
        event = {"context": context, "stage": "failed", "failure": self.failure()}
        api = SimpleNamespace(kernels_logs_stream=lambda ref: [{"data": "TRAFFIC_CONTROL_EVENT " + json.dumps(event)}])
        output = io.StringIO()
        operation(api, "events", {"reference": "private/notebook/1", "context": context}, output)
        packet = json.loads(output.getvalue().removeprefix("TRAFFIC_EXECUTION_EVENT "))
        self.assertEqual(packet["event"], {"control": "failed", "failure": self.failure()})

    def test_provider_journals_failure_separately_from_progress(self):
        provider = KaggleExecution.__new__(KaggleExecution)
        provider.stream = FakeStream([{"control": "failed", "failure": self.failure()}, {"control": "failed"}])
        provider.ledger = {}
        provider.last_sequence = 0
        provider.save = lambda **values: provider.ledger.update(copy.deepcopy(values))
        progress = Mock()
        provider.drain(progress)
        self.assertEqual(provider.ledger["failure_diagnostic"], self.failure())
        progress.assert_not_called()

    def test_known_child_failure_is_distinct_from_provider_failure(self):
        for known in (False, True):
            provider = KaggleExecution.__new__(KaggleExecution)
            provider.ledger = {"failure_diagnostic": self.failure()} if known else {}
            provider.clock = lambda: 1000
            provider.submit, provider.control, provider.drain, provider.sleep = Mock(), Mock(), Mock(), Mock()
            provider.status = lambda: "ERROR"
            provider.stream = None
            with self.subTest(known=known), self.assertRaises(ProviderError) as caught:
                provider.run(Mock(), lambda: False)
            self.assertEqual(caught.exception.category, "ml_execution_failed" if known else "execution_validation")


if __name__ == "__main__":
    unittest.main()
