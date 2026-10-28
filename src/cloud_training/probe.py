"""Prepare and validate a small, private Kaggle capability test.

This is an infrastructure probe using synthetic data, not a second production
training pipeline. Nothing in this module connects to Kaggle or opens a model.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import os
import time
import uuid


EVENT_PREFIX = "TRAFFIC_TRAINING_EVENT "
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
OUTPUT_FILES = {"model.keras", "preprocessor.json", "samples.npz", "report.json"}
STATES = {"QUEUED", "RUNNING", "COMPLETE", "ERROR", "CANCEL_REQUESTED", "CANCEL_ACKNOWLEDGED", "NEW_SCRIPT"}


class ProbeError(ValueError):
    """Safe diagnostic: never include credentials or a raw SDK exception."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(128 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path, limit=128 * 1024):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ProbeError("Probe JSON is missing, too large, or a symbolic link.")
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as error:
        raise ProbeError("Probe JSON is invalid.") from error


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical(value))
    temporary.replace(path)


def update_job(directory, changes):
    """Merge small PoC metadata updates without overwriting a concurrent cancel.

    A lock left by a crashed writer is not automatically removed; fail closed.
    Production coordination will use its own durable run lifecycle.
    """
    directory = Path(directory)
    lock = directory / "metadata.lock"
    deadline = time.monotonic() + 3
    while True:
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ProbeError("Probe metadata has an active or unresolved writer; no update was overwritten.") from None
            time.sleep(0.02)
    try:
        os.close(fd)
        job = read_json(directory / "job.json")
        updates = dict(changes)
        if "last_event_seq" in updates:
            updates["last_event_seq"] = max(job.get("last_event_seq", 0), updates["last_event_seq"])
        for field in ("progress_events_observed", "live_epoch_progress_observed", "cooperative_child_running_observed", "cooperative_cancel_ack_observed"):
            if field in updates:
                updates[field] = bool(job.get(field)) or bool(updates[field])
        job.update(updates)
        write_json(directory / "job.json", job)
        return job
    finally:
        lock.unlink()


def safe_status(value):
    state = getattr(value, "name", str(value)).upper().split(".")[-1]
    if state not in STATES:
        raise ProbeError("Kaggle returned an unrecognized job state.")
    return state


def prepare_probe(root, username, accelerator="cpu", purpose="smoke", observation_seconds=0):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{3,50}", username):
        raise ProbeError("Kaggle username is invalid.")
    if accelerator not in {"cpu", "gpu"} or purpose not in {"smoke", "cancel", "cooperative-cancel"}:
        raise ProbeError("Unsupported probe accelerator or purpose.")
    if purpose == "cooperative-cancel" and accelerator != "cpu":
        raise ProbeError("The cooperative cancellation capability probe is CPU-only.")
    if type(observation_seconds) is not int or not 0 <= observation_seconds <= 30:
        raise ProbeError("The observation delay must be between 0 and 30 seconds.")
    job_id = str(uuid.uuid4())
    directory = Path(root) / job_id
    directory.mkdir(parents=True, exist_ok=False)
    slug = "traffic-poc-" + uuid.UUID(job_id).hex[:16]
    config = {"schema": 1, "job_id": job_id, "accelerator": accelerator, "purpose": purpose,
              "observation_seconds": observation_seconds}
    if purpose == "cooperative-cancel":
        config["reference"] = f"{username}/{slug}"
    filename = "cooperative_probe_notebook.py" if purpose == "cooperative-cancel" else "probe_notebook.py"
    template = Path(__file__).with_name(filename).read_text(encoding="utf-8")
    source = template.replace("CONFIG = {}  # populated by prepare_probe", "CONFIG = " + repr(config), 1)
    (directory / "probe.py").write_text(source, encoding="utf-8")
    metadata = {
        "id": f"{username}/{slug}", "title": slug, "code_file": "probe.py",
        "language": "python", "kernel_type": "script", "is_private": True,
        "enable_gpu": accelerator == "gpu", "enable_internet": purpose == "cooperative-cancel",
        "dataset_sources": [], "competition_sources": [], "kernel_sources": [], "model_sources": [],
    }
    write_json(directory / "kernel-metadata.json", metadata)
    job = {**config, "reference": metadata["id"], "source_sha256": sha256_file(directory / "probe.py"),
           "metadata_sha256": sha256_file(directory / "kernel-metadata.json"), "submitted": False,
           "session_identity_verified": False, "cancel_confirmed": False}
    write_json(directory / "job.json", job)
    return directory, job


def load_job(directory):
    directory = Path(directory).resolve(strict=True)
    job = read_json(directory / "job.json")
    try:
        if str(uuid.UUID(job["job_id"])) != job["job_id"]:
            raise ValueError()
        if type(job["schema"]) is not int or job["schema"] != 1 or job["accelerator"] not in {"cpu", "gpu"} or job["purpose"] not in {"smoke", "cancel", "cooperative-cancel"}:
            raise ValueError()
        if not re.fullmatch(r"[a-zA-Z0-9_-]{3,50}/traffic-poc-[0-9a-f]{16}", job["reference"]):
            raise ValueError()
    except (KeyError, ValueError, TypeError) as error:
        raise ProbeError("Invalid probe job identity.") from error
    if (sha256_file(directory / "probe.py") != job["source_sha256"]
            or sha256_file(directory / "kernel-metadata.json") != job["metadata_sha256"]):
        raise ProbeError("Probe code or metadata changed after preparation. Prepare a new job.")
    metadata = read_json(directory / "kernel-metadata.json")
    if metadata.get("id") != job["reference"] or metadata.get("is_private") is not True:
        raise ProbeError("Probe identity/privacy mismatch.")
    return directory, job


def versioned_reference(job):
    version = job.get("version")
    if job.get("submitted") is not True or type(version) is not int or version < 1:
        raise ProbeError("The job has no confirmed Kaggle version. Do not resubmit an uncertain job.")
    return f"{job['reference']}/{version}"


def separate_sdk_log(directory, job):
    """The SDK writes a notebook .log even when file_pattern is restrictive.

    Keep that known diagnostic outside the manifest-verified model directory.
    This function never accepts extra files as model artifacts or removes them.
    """
    directory = Path(directory).resolve(strict=True)
    filename = job["reference"].split("/")[1] + ".log"
    source = directory / "output" / filename
    if not source.exists() and not source.is_symlink():
        return
    target = directory / "provider.log"
    if (source.is_symlink() or not source.is_file() or source.stat().st_size > 128 * 1024
            or target.exists() or target.is_symlink()
            or source.parent.is_symlink()):
        raise ProbeError("SDK diagnostic log cannot be isolated safely; output was not accepted.")
    source.rename(target)


def parse_event(line, job):
    """Accept only our structured probe events, not arbitrary notebook output."""
    if not isinstance(line, str) or len(line) > 8192 or not line.startswith(EVENT_PREFIX):
        return None
    try:
        event = json.loads(line[len(EVENT_PREFIX):])
        allowed = {"schema", "job_id", "seq", "stage", "model", "epoch", "epochs", "gpu_count", "loss"}
        if (not isinstance(event, dict) or set(event) - allowed or type(event.get("schema")) is not int or event.get("schema") != 1
                or event.get("job_id") != job["job_id"] or type(event.get("seq")) is not int
                or event["seq"] < 1 or event.get("stage") not in {
                    "starting", "device_ready", "epoch_started", "epoch_completed", "saving",
                    "completed", "waiting_for_cancel", "cancel_not_observed", "failed",
                    "control_ready", "child_running", "cancelled"}):
            raise ValueError()
        model = "ProcessProbe" if job["purpose"] == "cooperative-cancel" else "LSTM"
        if event.get("model", model) != model:
            raise ValueError()
        for key in ("epoch", "epochs", "gpu_count"):
            if key in event and (type(event[key]) is not int or not 0 <= event[key] <= 10000):
                raise ValueError()
        if "loss" in event:
            import math
            if type(event["loss"]) not in (int, float) or not math.isfinite(event["loss"]):
                raise ValueError()
        return event
    except (ValueError, TypeError):
        raise ProbeError("Invalid progress event; raw output omitted.") from None


def verify_output(directory, job):
    root = Path(directory).resolve(strict=True)
    manifest = read_json(root / "manifest.json")
    if type(manifest.get("schema")) is not int or manifest.get("schema") != 1 or manifest.get("job_id") != job["job_id"] or job["purpose"] != "smoke":
        raise ProbeError("Output belongs to another job or is not a successful smoke test.")
    items = manifest.get("files")
    if not isinstance(items, list) or len(items) != len(OUTPUT_FILES):
        raise ProbeError("Output manifest has missing or unexpected files.")
    seen, total = set(), 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ProbeError("Invalid output file metadata.")
        name = item["path"]
        if (not isinstance(name, str) or name not in OUTPUT_FILES or name in seen
                or PurePosixPath(name).name != name):
            raise ProbeError("Unsafe, duplicate, or unexpected output path.")
        seen.add(name)
        size, digest = item["size"], item["sha256"]
        if type(size) is not int or size < 1 or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise ProbeError("Invalid output size/checksum.")
        total += size
        if total > MAX_OUTPUT_BYTES:
            raise ProbeError("Probe output exceeds its size limit.")
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size or sha256_file(path) != digest:
            raise ProbeError("Probe output is missing, corrupted, or a symbolic link.")
    actual = {p.name for p in root.iterdir()}
    if actual != OUTPUT_FILES | {"manifest.json"}:
        raise ProbeError("Unexpected files in probe output directory.")
    report = read_json(root / "report.json")
    if (report.get("job_id") != job["job_id"] or report.get("status") != "completed"
            or type(report.get("gpu_count")) is not int
            or report["gpu_count"] < 0 or report.get("synthetic_only") is not True
            or (job["accelerator"] == "gpu" and report["gpu_count"] < 1)):
        raise ProbeError("Probe completion or GPU evidence is missing.")
    preprocessing = read_json(root / "preprocessor.json")
    if preprocessing != {"schema": 1, "kind": "identity", "sequence_length": 6, "features": 2}:
        raise ProbeError("The synthetic probe preprocessing contract is inconsistent.")
    return {"job_id": job["job_id"], "files_verified": len(seen), "bytes_verified": total,
            "gpu_count": report["gpu_count"], "output_verified": True,
            "cpu_reload_verified": False, "production_ready": False,
            "reason": "Production requires CPU reload, live progress and confirmed automatic cancellation proofs."}
