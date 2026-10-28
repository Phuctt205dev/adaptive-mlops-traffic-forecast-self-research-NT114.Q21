"""Run-scoped child processes and cleanup; never terminate a pod or API."""
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import threading
import uuid


PREFIX = "TRAFFIC_EXECUTION_EVENT "


class ExecutionProcess:
    def __init__(self, command, context, *, cwd=None, stdin=None):
        self.context = context
        self.events = queue.Queue(maxsize=1000)
        self.reader_failed = False
        environment = dict(os.environ)
        environment["TRAFFIC_EXECUTION_PARENT_PID"] = str(os.getpid())
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                    "TF_NUM_INTEROP_THREADS", "TF_NUM_INTRAOP_THREADS"):
            environment[key] = "1"
        self.process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=stdin, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                        start_new_session=os.name != "nt",
                                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                # Never retain arbitrary stderr/SDK output containing paths or
                # credentials. Only the structured run-scoped protocol passes.
                if not line.startswith(PREFIX):
                    continue
                if len(line) > 16384:
                    raise ValueError("oversized_progress")
                item = json.loads(line[len(PREFIX):])
                if set(item) != {"context", "event"} or item["context"] != self.context:
                    raise ValueError("progress_context_mismatch")
                self.events.put_nowait(item["event"])
        except (ValueError, OSError, queue.Full):
            self.reader_failed = True

    def drain(self):
        result = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result

    def returncode(self):
        code = self.process.poll()
        if code is not None:
            self.reader.join(timeout=1)
        return code

    def stop(self):
        if self.process.poll() is None:
            if os.name == "nt":
                self.process.terminate()
            else:
                if os.getpgid(self.process.pid) != self.process.pid:
                    raise RuntimeError("The execution process group is not owned by this controller.")
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    self.process.kill()
                else:
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close()
        return self.process.poll() is not None


def execution_directory(root, run_id, branch, attempt_id):
    for value in (run_id, attempt_id):
        if str(uuid.UUID(str(value))) != str(value):
            raise ValueError("Invalid run/attempt identity.")
    if branch not in {"tree", "lstm", "gru"}:
        raise ValueError("Invalid branch identity.")
    root = Path(root)
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("Execution root must not be a symlink.")
    return root.absolute() / str(run_id) / branch / str(attempt_id)


def cleanup_execution(root, run_id, branch, attempt_id):
    expected = execution_directory(root, run_id, branch, attempt_id)
    if not expected.exists() and not expected.is_symlink():
        return True
    if any(part.is_symlink() for part in (expected, *expected.parents)):
        raise ValueError("Cleanup must not follow a symbolic link.")
    resolved_root, resolved_target = Path(root).resolve(strict=True), expected.resolve(strict=True)
    if resolved_target == resolved_root or not resolved_target.is_relative_to(resolved_root):
        raise ValueError("Cleanup target is outside the execution staging root.")
    # The resolved absolute target has been checked, including on Windows.
    shutil.rmtree(resolved_target)
    return not expected.exists()
