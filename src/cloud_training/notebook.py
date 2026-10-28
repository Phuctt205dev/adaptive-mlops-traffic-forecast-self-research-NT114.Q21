"""Kaggle supervisor template. Only CONFIG and the final control line vary.

The platform credential stays on Kaggle. The ML child receives a verified
private snapshot and cannot publish business data to our database or registry.
"""
CONFIG = {}


def main():
    import ast
    import contextlib
    import hashlib
    import io
    import json
    import os
    from pathlib import Path
    import queue
    import shutil
    import signal
    import subprocess
    import sys
    import tempfile
    import threading
    import time
    import zipfile

    child = None
    output = Path("/kaggle/working/output")
    sequence = 0
    queue_events = queue.Queue(maxsize=1000)
    reader_failed = threading.Event()

    def emit(stage):
        nonlocal sequence
        sequence += 1
        print("TRAFFIC_CONTROL_EVENT " + json.dumps({"context": CONFIG["context"], "sequence": sequence,
                                                   "stage": stage}), flush=True)

    def stop():
        if child is not None and child.poll() is None:
            if os.getpgid(child.pid) != child.pid:
                raise RuntimeError("Unowned execution process group.")
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)

    def safe_name(name):
        if (not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name
                or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/"))):
            raise ValueError("Unsafe snapshot path.")
        return name

    def read_output():
        try:
            for line in child.stdout:
                if line.startswith("TRAFFIC_EXECUTION_EVENT "):
                    if len(line) > 16384:
                        raise ValueError("Oversized progress event.")
                    event = json.loads(line[len("TRAFFIC_EXECUTION_EVENT "):])
                    if set(event) != {"context", "event"} or event["context"] != CONFIG["context"]:
                        raise ValueError("Wrong progress context.")
                    queue_events.put_nowait(line.rstrip())
        except Exception:
            reader_failed.set()

    try:
        import requests
        original_send = requests.Session.send
        def bounded_send(session, request, **kwargs):
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = (5, 10)
            return original_send(session, request, **kwargs)
        requests.Session.send = bounded_send
        # Never send a local token. Kaggle supplies its notebook credential.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from kaggle.api.kaggle_api_extended import KaggleApi
            from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest
            api = KaggleApi()
            api.authenticate()
        owner, slug = CONFIG["reference"].split("/")

        def check_control():
            request = ApiGetKernelRequest()
            request.user_name, request.kernel_slug = owner, slug
            with api.build_kaggle_client() as client:
                remote = client.kernels.kernels_api_client.get_kernel(request)
            source = remote.blob.source.replace("\r\n", "\n")
            prefix = "# TRAFFIC_CONTROL "
            lines = source.splitlines()
            controls = [line[len(prefix):] for line in lines if line.startswith(prefix)]
            config_lines = [line[len("CONFIG = "):] for line in lines if line.startswith("CONFIG = ")]
            body = "\n".join(line for line in lines if not line.startswith((prefix, "CONFIG = "))).rstrip() + "\n"
            if (remote.metadata.ref != CONFIG["reference"] or remote.metadata.is_private is not True
                    or len(controls) != 1 or len(config_lines) != 1 or ast.literal_eval(config_lines[0]) != CONFIG
                    or hashlib.sha256(body.encode()).hexdigest() != CONFIG["body_sha256"]):
                raise RuntimeError("Control source identity differs.")
            control = json.loads(controls[0])
            if (set(control) != {"attempt_id", "cancel", "lease_until"}
                    or control["attempt_id"] != CONFIG["context"]["attempt_id"]
                    or type(control["cancel"]) is not bool
                    or type(control["lease_until"]) not in (int, float)
                    or not time.time() < control["lease_until"] <= time.time() + 300):
                return False
            return not control["cancel"]

        if not check_control():
            emit("cancelled")
            return 0
        emit("control_ready")
        dataset_slug = CONFIG["dataset_reference"].split("/")[1]
        inputs = Path("/kaggle/input") / dataset_slug
        # Newer mount layouts can include datasets/<owner>/<slug>.
        if not inputs.is_dir():
            matches = [item.parent for item in Path("/kaggle/input").rglob("job.json")
                       if item.parent.name == dataset_slug]
            if len(matches) != 1:
                raise RuntimeError("Private input snapshot not found unambiguously.")
            inputs = matches[0]
        for name, descriptor in CONFIG["files"].items():
            path = inputs / safe_name(name)
            if path.is_symlink() or not path.is_file() or path.stat().st_size != descriptor["size"]:
                raise RuntimeError("Input snapshot file differs.")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != descriptor["sha256"]:
                raise RuntimeError("Input snapshot checksum differs.")
        with tempfile.TemporaryDirectory(prefix="traffic-execution-") as directory:
            code = Path(directory)
            with zipfile.ZipFile(inputs / "source.bin") as archive:
                names = set()
                if len(archive.infolist()) > 100 or sum(item.file_size for item in archive.infolist()) > 4 * 1024 * 1024:
                    raise RuntimeError("Source snapshot exceeds budget.")
                for item in archive.infolist():
                    name = safe_name(item.filename)
                    if name in names or not name.endswith(".py") or item.is_dir() or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                        raise RuntimeError("Invalid source snapshot member.")
                    names.add(name)
                    target = code / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(item))
            environment = dict(os.environ)
            for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                        "TF_NUM_INTEROP_THREADS", "TF_NUM_INTRAOP_THREADS"):
                environment[key] = "1"
            child = subprocess.Popen([sys.executable, "-u", str(code / "scripts/cloud_training/execute_branch.py"),
                                      "--job", str(inputs / "job.json"), "--dataset", str(inputs / "dataset.csv"),
                                      "--output", str(output)], cwd=code, env=environment,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", start_new_session=True)
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            emit("child_running")
            deadline = time.monotonic() + 8 * 3600
            while True:
                if not check_control():
                    stop()
                    if output.exists():
                        shutil.rmtree(output)  # Fixed owned directory, after child exit.
                    emit("cancelled")
                    return 0
                while not queue_events.empty():
                    print(queue_events.get_nowait(), flush=True)
                if reader_failed.is_set() or time.monotonic() >= deadline:
                    raise RuntimeError("Execution monitoring failed or exceeded deadline.")
                if child.poll() is not None:
                    reader.join(timeout=2)
                    while not queue_events.empty():
                        print(queue_events.get_nowait(), flush=True)
                    if child.returncode != 0 or reader_failed.is_set() or not (output / "manifest.json").is_file():
                        raise RuntimeError("ML child did not produce validated output.")
                    emit("child_completed")
                    return 0
                time.sleep(3)
    except Exception:
        stop()
        if output.exists():
            shutil.rmtree(output)
        emit("failed")
        return 2
    finally:
        stop()


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
