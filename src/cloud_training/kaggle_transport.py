"""Bound SDK/network calls in a separate process; never expose provider errors."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from src.cloud_training.process import ExecutionProcess


class ProviderError(RuntimeError):
    def __init__(self, message, http_status=None, *, operation=None, category=None):
        super().__init__(message)
        self.http_status = http_status
        self.operation, self.category = operation or "execution", category or "execution_validation"


class SDKTransport:
    READ_OPERATIONS = frozenset({"doctor", "status", "kernel_get", "dataset_check", "output_list",
                                 "resource_exists", "owned_datasets", "owned_notebooks"})
    def __init__(self, username, *, python=None, repository=None):
        self.username = username
        self.python = str(python or sys.executable)
        self.repository = Path(repository or Path(__file__).resolve().parents[2])

    def payload(self, operation, **values):
        return {"username": self.username, "operation": operation, **values}

    def call(self, operation, *, timeout=75, **values):
        deadline = time.monotonic() + timeout
        attempts = 3 if operation in self.READ_OPERATIONS else 1
        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("Kaggle operation exceeded its deadline.", operation=operation, category="rpc_timeout")
            try:
                return self.once(operation, timeout=remaining, **values)
            except ProviderError as error:
                transient = (error.category in {"connect_timeout", "connection_error", "response_timeout", "rpc_timeout", "rpc_unavailable"}
                             or error.http_status in {408, 429, 500, 502, 503, 504})
                delay = 0.5 * (attempt + 1)
                if not transient or attempt + 1 == attempts or deadline - time.monotonic() <= delay:
                    raise
                time.sleep(delay)

    def once(self, operation, *, timeout, **values):
        try:
            result = subprocess.run([self.python, "-B", "-m", "src.cloud_training.kaggle_rpc"],
                input=json.dumps(self.payload(operation, **values)), text=True, encoding="utf-8",
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=self.repository, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if len(result.stdout) > 2 * 1024 * 1024:
                raise ProviderError("Kaggle operation could not be confirmed.")
            document = json.loads(result.stdout)
            if not isinstance(document, dict):
                raise ValueError("Invalid RPC envelope.")
            if result.returncode or document.get("ok") is not True:
                status = document.get("http_status")
                category = {"ConnectTimeout": "connect_timeout", "ReadTimeout": "response_timeout",
                            "ConnectionError": "connection_error", "ChunkedEncodingError": "connection_error",
                            "HTTPError": "http_error", "ValueError": "invalid_response"}.get(document.get("error_type"), "provider_error")
                raise ProviderError("Kaggle operation could not be confirmed.", status if type(status) is int else None,
                                    operation=operation, category=category)
            return document["result"]
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            category = "rpc_timeout" if isinstance(error, subprocess.TimeoutExpired) else "invalid_response" if isinstance(error, ValueError) else "rpc_unavailable"
            raise ProviderError("Kaggle operation could not be confirmed within its deadline.", operation=operation, category=category) from None

    def stream(self, reference, context):
        # stdin is a private pipe, never a token or dataset in command arguments.
        child = ExecutionProcess([self.python, "-u", "-B", "-m", "src.cloud_training.kaggle_rpc"],
                                 context, cwd=self.repository, stdin=subprocess.PIPE)
        child.process.stdin.write(json.dumps(self.payload("events", reference=reference, context=context)))
        child.process.stdin.close()
        return child
