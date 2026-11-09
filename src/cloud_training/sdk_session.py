"""One bounded RPC subprocess per attempt, never shared with a sibling fit."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

PREFIX = "TRAFFIC_RPC_RESPONSE "
MAX_RESPONSE = 2 * 1024 * 1024
MAX_REQUEST = 1024 * 1024
MAX_SESSION_REQUESTS = 512


class SessionError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__("The scoped SDK response could not be confirmed.")


class RPCSession:
    def __init__(self, command, repository):
        self.command, self.repository = command, Path(repository)
        self.lock = threading.RLock()
        self.process = self.reader = None
        self.responses = None
        self.completed = 0

    def _start(self):
        self.process = subprocess.Popen(self.command, cwd=self.repository, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.responses = queue.Queue(maxsize=2)
        process, responses = self.process, self.responses
        self.completed = 0
        def read():
            try:
                while True:
                    line = process.stdout.readline(MAX_RESPONSE + 1)
                    if not line or len(line) > MAX_RESPONSE:
                        break
                    if line.startswith(PREFIX):
                        responses.put_nowait(line[len(PREFIX):])
            except (OSError, ValueError, queue.Full):
                pass
            finally:
                try:
                    responses.put_nowait(None)
                except queue.Full:
                    pass
        self.reader = threading.Thread(target=read, name="kaggle-sdk-replies", daemon=True)
        self.reader.start()

    def request(self, payload, timeout):
        deadline = time.monotonic() + timeout
        if not self.lock.acquire(timeout=max(0, timeout)):
            raise SessionError("rpc_timeout")
        writer = None
        try:
            if self.process and (self.process.poll() is not None or self.completed >= MAX_SESSION_REQUESTS):
                self.close()
            if self.process is None:
                self._start()
            identity = uuid.uuid4().hex
            line = json.dumps({**payload, "request_id": identity}, allow_nan=False) + "\n"
            if len(line.encode("utf-8")) > MAX_REQUEST:
                raise SessionError("invalid_response")
            process, responses = self.process, self.responses
            def write():
                try:
                    process.stdin.write(line)
                    process.stdin.flush()
                except (OSError, ValueError):
                    try:
                        responses.put_nowait(None)
                    except queue.Full:
                        pass
            # Even a blocked pipe write is covered by the response deadline.
            writer = threading.Thread(target=write, name="kaggle-sdk-request", daemon=True)
            writer.start()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionError("rpc_timeout")
            raw = responses.get(timeout=remaining)
            if raw is None:
                raise SessionError("rpc_unavailable")
            document = json.loads(raw)
            if not isinstance(document, dict) or document.get("request_id") != identity:
                raise SessionError("invalid_response")
            self.completed += 1
            return document
        except queue.Empty:
            self.close()
            raise SessionError("rpc_timeout") from None
        except (OSError, ValueError):
            self.close()
            raise SessionError("rpc_unavailable") from None
        except SessionError:
            self.close()
            raise
        finally:
            if writer:
                writer.join(timeout=1)
            self.lock.release()

    def close(self):
        with self.lock:
            process = self.process
            if process is None:
                return
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)
            if self.reader:
                self.reader.join(timeout=1)
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            self.process = self.reader = self.responses = None
