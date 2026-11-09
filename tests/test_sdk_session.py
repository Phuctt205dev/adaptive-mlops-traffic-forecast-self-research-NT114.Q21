"""Offline process/protocol checks; no Kaggle credentials or network calls."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.cloud_training.kaggle_rpc import EventRelay, serve, stream_events
from src.cloud_training.kaggle_transport import ProviderError, SDKTransport
from src.cloud_training.sdk_session import PREFIX, RPCSession, SessionError


WORKER = '''import json,os,sys,time
count=0
for line in sys.stdin:
 request=json.loads(line);count+=1
 if request.get('operation')=='hang':time.sleep(10)
 identity=request['request_id'] if request.get('operation')!='wrong' else '0'*32
 print('unstructured SDK noise',flush=True)
 print('TRAFFIC_RPC_RESPONSE '+json.dumps({'request_id':identity,'ok':True,
  'result':{'count':count,'pid':os.getpid()},'session_reused':count>1}),flush=True)
'''


class SDKSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        script = root / "offline_sdk.py"
        script.write_text(WORKER, encoding="utf-8")
        self.session = RPCSession([sys.executable, "-u", "-B", str(script)], root)
        self.addCleanup(self.session.close)

    def test_repeated_requests_reuse_process_and_ignore_unstructured_noise(self):
        first = self.session.request({"operation": "status"}, 5)
        second = self.session.request({"operation": "kernel_get"}, 5)
        self.assertEqual(first["result"]["pid"], second["result"]["pid"])
        self.assertEqual(second["result"]["count"], 2)
        self.assertTrue(second["session_reused"])
        self.assertNotEqual(first["request_id"], second["request_id"])

    def test_rotation_happens_between_requests_without_repeating_an_operation(self):
        with patch("src.cloud_training.sdk_session.MAX_SESSION_REQUESTS", 2):
            replies = [self.session.request({"operation": "status"}, 5) for _ in range(3)]
        self.assertEqual([r["result"]["count"] for r in replies], [1, 2, 1])
        self.assertNotEqual(replies[0]["result"]["pid"], replies[-1]["result"]["pid"])

    def test_unconfirmed_write_timeout_kills_process_and_is_not_retried(self):
        transport = SDKTransport("synthetic", persistent=True)
        transport.session = self.session
        original = self.session.request
        calls = []
        def request(payload, timeout):
            calls.append(payload["operation"])
            return original({**payload, "operation": "hang"}, timeout)
        with patch.object(self.session, "request", side_effect=request):
            with self.assertRaises(ProviderError) as caught:
                transport.call("kernel_push", timeout=.5)
        self.assertEqual(caught.exception.category, "rpc_timeout")
        self.assertEqual(calls, ["kernel_push"])
        self.assertIsNone(self.session.process)
        self.assertEqual(original({"operation": "status"}, 5)["result"]["count"], 1)

    def test_wrong_response_identity_is_fenced_and_process_closed(self):
        with self.assertRaises(SessionError) as caught:
            self.session.request({"operation": "wrong"}, 5)
        self.assertEqual(caught.exception.category, "invalid_response")
        self.assertIsNone(self.session.process)

    def test_transport_forks_have_separate_sessions_and_keep_runtime(self):
        parent = SDKTransport("synthetic", python=sys.executable, persistent=True)
        child = parent.fork()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        self.assertIsNot(parent.session, child.session)
        self.assertEqual(parent.python, child.python)
        self.assertEqual(parent.repository, child.repository)

    def test_transient_read_can_retry_but_invalid_or_ambiguous_write_cannot(self):
        transport = SDKTransport("synthetic", persistent=True)
        self.addCleanup(transport.close)
        success = {"ok": True, "result": {"state": "QUEUED"}, "session_reused": False,
                   "timing": {"sdk_import_auth": .1, "sdk_operation": .2}}
        with patch.object(transport.session, "request", side_effect=[SessionError("rpc_unavailable"), success]) as request, \
             patch("src.cloud_training.kaggle_transport.time.sleep"):
            self.assertEqual(transport.call("status")["state"], "QUEUED")
            self.assertEqual(request.call_count, 2)
        for operation in ("kernel_push", "dataset_create", "cleanup"):
            with self.subTest(operation=operation), \
                 patch.object(transport.session, "request", side_effect=SessionError("rpc_unavailable")) as request:
                with self.assertRaises(ProviderError):
                    transport.call(operation)
                self.assertEqual(request.call_count, 1)

    def test_server_authenticates_once_and_never_exposes_provider_exception_text(self):
        factory = Mock(return_value=object())
        handler = Mock(side_effect=[{"state": "QUEUED"}, {"state": "RUNNING"}, ValueError("PRIVATE_TOKEN")])
        payloads = [{"request_id": str(n) * 32, "username": "synthetic", "operation": "status"} for n in (1, 2, 3)]
        output = io.StringIO()
        serve(io.StringIO("".join(json.dumps(p) + "\n" for p in payloads)), output, api_factory=factory, handler=handler)
        replies = [json.loads(line.removeprefix(PREFIX)) for line in output.getvalue().splitlines()]
        factory.assert_called_once_with("synthetic")
        self.assertEqual(handler.call_count, 3)
        self.assertFalse(replies[0]["session_reused"])
        self.assertTrue(replies[1]["session_reused"])
        self.assertEqual(replies[1]["timing"]["sdk_import_auth"], 0)
        self.assertFalse(replies[2]["ok"])
        self.assertNotIn("PRIVATE_TOKEN", output.getvalue())

    def test_server_rejects_username_change_and_stream_on_rpc_channel(self):
        payloads = [{"request_id": str(n) * 32, "username": user, "operation": operation}
                    for n, user, operation in ((1, "first", "status"), (2, "other", "status"), (3, "first", "events"))]
        output, handler = io.StringIO(), Mock(return_value={"state": "QUEUED"})
        serve(io.StringIO("".join(json.dumps(p) + "\n" for p in payloads)), output,
              api_factory=Mock(return_value=object()), handler=handler)
        replies = [json.loads(line.removeprefix(PREFIX)) for line in output.getvalue().splitlines()]
        self.assertEqual([r["ok"] for r in replies], [True, False, False])
        handler.assert_called_once()

    def test_log_reconnect_reuses_api_and_deduplicates_replayed_progress(self):
        output, api, elapsed = io.StringIO(), object(), [0]
        request = {"context": {"attempt_id": "synthetic"}, "reference": "synthetic/notebook/1"}
        def operation(current, name, payload, relay):
            self.assertIs(current, api)
            self.assertEqual(payload["reference"], request["reference"])
            for event in ({"sequence": 1}, {"control": "child_running", "worker_stream_time": str(elapsed[0])}):
                relay.write("TRAFFIC_EXECUTION_EVENT " + json.dumps({"context": request["context"], "event": event}) + "\n")
        with patch("src.cloud_training.kaggle_rpc.operation", side_effect=operation) as calls:
            stream_events(api, request, output, clock=lambda: elapsed[0],
                          sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + 8 * 3600 / 3))
        self.assertEqual(calls.call_count, 3)
        self.assertEqual(len(output.getvalue().splitlines()), 2)

    def test_log_context_mismatch_is_never_forwarded(self):
        output = io.StringIO()
        relay = EventRelay(output, {"attempt_id": "expected"})
        with self.assertRaises(ValueError):
            relay.write('TRAFFIC_EXECUTION_EVENT {"context":{},"event":{"sequence":1}}\n')
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
