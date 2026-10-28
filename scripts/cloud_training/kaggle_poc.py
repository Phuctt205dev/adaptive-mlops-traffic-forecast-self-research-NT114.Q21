"""Explicit Kaggle setup/probe commands. Never imports or triggers the app."""
from __future__ import annotations

import argparse
import contextlib
from datetime import UTC, datetime
import getpass
import importlib.metadata
import io
import json
import multiprocessing
import os
from pathlib import Path
import queue
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.cloud_training.probe import ProbeError, load_job, prepare_probe, separate_sdk_log, update_job, verify_output, write_json
from src.cloud_training.kaggle_probe_client import KaggleProbeClient


def output(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def credential_available():
    folder = Path.home() / ".kaggle"
    return bool(os.environ.get("KAGGLE_API_TOKEN")) or any(
        (folder / name).is_file() for name in ("access_token", "access_token.txt", "kaggle.json")
    ) or bool(os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY"))


def sdk_api(expected_username=None):
    if not credential_available():
        raise ProbeError("No Kaggle credential configured. Run setup-kaggle.ps1 -Action Login; never paste a token into chat.")
    # Kaggle's package initializes an API client on import. Prevent its
    # interactive help or authentication output from entering diagnostics.
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from kaggle.api.kaggle_api_extended import KaggleApi
            api = KaggleApi()
            api.authenticate()
    except (Exception, SystemExit):
        raise ProbeError("Kaggle authentication failed. Check the token, account permissions and Internet connection.") from None
    actual = api.config_values.get(api.CONFIG_NAME_USER)
    if expected_username and actual != expected_username:
        raise ProbeError("The authenticated Kaggle account does not match the requested username.")
    return api


def doctor(username, online=False):
    versions = {}
    for package in ("kaggle", "kagglesdk"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    report = {"checked_at": datetime.now(UTC).isoformat(), "username_expected": username,
              "sdk_versions": versions, "credential_configured": credential_available(),
              "online_checked": False, "production_ready": False}
    if online:
        api = sdk_api(username)
        quota = api.quota_view()
        gpu = quota.gpu_quota
        report.update(online_checked=True, authenticated=True,
                      gpu_hours_remaining=max(0.0, (gpu.total_time_allowed - gpu.time_used).total_seconds() / 3600)
                      if gpu else None,
                      quota_refresh_at=quota.quota_refresh_time.isoformat() if quota.quota_refresh_time else None)
    return report


def login(username):
    # Run interactively by the user; token is neither a command-line argument
    # nor stored inside the project or generated notebook.
    if not sys.stdin.isatty():
        raise ProbeError("Login requires a private interactive terminal.")
    target = Path.home() / ".kaggle" / "access_token"
    if target.exists() or target.is_symlink() or target.parent.is_symlink():
        raise ProbeError("An access_token already exists or the credential path is a link. It has not been overwritten.")
    token = getpass.getpass("Paste Kaggle API token (hidden; not sent to chat): ").strip()
    if not token or any(character.isspace() for character in token):
        raise ProbeError("The token is empty or contains whitespace.")
    prior = os.environ.get("KAGGLE_API_TOKEN")
    try:
        os.environ["KAGGLE_API_TOKEN"] = token
        sdk_api(username)  # Validate identity before saving anything.
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token)
        if os.name == "nt":
            identity = subprocess.check_output(["whoami"], text=True).strip()
            result = subprocess.run(["icacls", str(target), "/inheritance:r", "/grant:r", identity + ":(F)"],
                                    capture_output=True, text=True)
            if result.returncode:
                target.unlink()  # Only the credential file this call created.
                raise ProbeError("Could not restrict token file permissions; the newly created file was removed.")
        return {"authenticated": True, "username": username, "token_saved_outside_project": True,
                "token_path": str(target), "job_submitted": False}
    finally:
        if prior is None:
            os.environ.pop("KAGGLE_API_TOKEN", None)
        else:
            os.environ["KAGGLE_API_TOKEN"] = prior


def stream_worker(directory, messages):
    try:
        _, job = load_job(directory)
        client = KaggleProbeClient(sdk_api(job["reference"].split("/")[0]))
        for event in client.events(directory):
            state = "NOT_CHECKED"
            if event["stage"] in {"starting", "epoch_completed", "child_running"}:
                try:
                    from src.cloud_training.probe import safe_status, versioned_reference
                    state = safe_status(client.api.kernels_status(versioned_reference(job)).status)
                except Exception:
                    state = "MONITORING_UNAVAILABLE"
            messages.put({"event": event, "provider_state": state}, timeout=1)
    except Exception:
        messages.put({"stream_error": True}, timeout=1)


def show_logs(directory, seconds):
    if not 1 <= seconds <= 120:
        raise ProbeError("A live log window must be between 1 and 120 seconds.")
    directory, job = load_job(directory)
    context = multiprocessing.get_context("spawn")
    messages = context.Queue(maxsize=100)
    process = context.Process(target=stream_worker, args=(str(directory), messages), daemon=True)
    last_seq = int(job.get("last_event_seq", 0))
    observed = False
    live_observed = False
    child_running_observed = False
    cancel_ack_observed = False
    process.start()
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            try:
                message = messages.get(timeout=min(0.5, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                if not process.is_alive():
                    break
                continue
            if message.get("stream_error"):
                output({"monitoring_unavailable": True, "training_failed": False})
                break
            event = message["event"]
            if event["seq"] <= last_seq:
                continue
            last_seq = event["seq"]
            observed = True
            live_observed = live_observed or (event["stage"] == "epoch_completed" and message["provider_state"] == "RUNNING")
            child_running_observed = child_running_observed or (event["stage"] == "child_running" and message["provider_state"] == "RUNNING")
            cancel_ack_observed = cancel_ack_observed or event["stage"] == "cancelled"
            recorded = {**event, "provider_state_at_observation": message["provider_state"]}
            output(recorded)
            with (directory / "progress.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(recorded, allow_nan=False) + "\n")
    finally:
        if process.is_alive():
            process.terminate()  # Our local log-reader process, NOT the Kaggle job.
        process.join(timeout=3)
        if process.is_alive():
            process.kill()
            process.join(timeout=3)
        messages.close()
        update_job(directory, {"last_event_seq": last_seq, "progress_events_observed": observed,
                               "live_epoch_progress_observed": live_observed,
                               "cooperative_child_running_observed": bool(job.get("cooperative_child_running_observed")) or child_running_observed,
                               "cooperative_cancel_ack_observed": cancel_ack_observed})
    return {"progress_events_observed_in_window": observed, "live_epoch_progress_observed": live_observed,
            "log_reader_did_not_send_cancel": True}


def reload_cpu(directory):
    # Only reload this probe's own verified small output. No application data.
    directory, job = load_job(directory)
    report = verify_output(directory / "output", job)
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    try:
        import numpy as np
        import tensorflow as tf
    except ImportError:
        raise ProbeError("CPU reload needs TensorFlow in a separate compatible test environment; it is not installed automatically.") from None
    import zipfile
    with zipfile.ZipFile(directory / "output" / "samples.npz") as archive:
        if sum(item.file_size for item in archive.infolist()) > 1024 * 1024:
            raise ProbeError("Expanded probe sample data exceeds its limit.")
    model = tf.keras.models.load_model(directory / "output" / "model.keras", safe_mode=True, compile=False)
    with np.load(directory / "output" / "samples.npz", allow_pickle=False) as samples:
        values, expected = samples["inputs"], samples["predictions"]
        if values.shape != (4, 6, 2) or expected.shape != (4, 1):
            raise ProbeError("Unexpected probe sample shapes.")
        actual = model(values, training=False).numpy()
    if not np.allclose(actual, expected, rtol=1e-4, atol=1e-5):
        raise ProbeError("CPU predictions differ from Kaggle beyond the probe tolerance.")
    report["cpu_reload_verified"] = True
    write_json(directory / "verification.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("doctor", "login", "prepare", "submit", "reconcile", "status", "logs", "fetch", "verify", "reload-cpu", "cancel", "cooperative-cancel", "confirm-cancel"))
    parser.add_argument("--username", default="truongphucdev")
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--job")
    parser.add_argument("--accelerator", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument("--purpose", choices=("smoke", "cancel", "cooperative-cancel"), default="smoke")
    parser.add_argument("--observation-seconds", type=int, default=0)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--session-id", type=int)
    parser.add_argument("--confirm-reference")
    args = parser.parse_args(argv)
    try:
        if args.action == "doctor":
            result = doctor(args.username, args.online)
        elif args.action == "login":
            result = login(args.username)
        elif args.action == "prepare":
            directory, job = prepare_probe(ROOT / "results/kaggle-poc/jobs", args.username, args.accelerator, args.purpose,
                                          args.observation_seconds)
            result = {"job_directory": str(directory), "reference": job["reference"], "private": True,
                      "synthetic_only": True, "submitted": False}
        else:
            if not args.job:
                raise ProbeError("--job is required for this action.")
            directory, job = load_job(args.job)
            if args.action == "verify":
                separate_sdk_log(directory, job)
                result = verify_output(directory / "output", job)
                write_json(directory / "verification.json", result)
            elif args.action == "reload-cpu":
                result = reload_cpu(directory)
            elif args.action == "logs":
                result = show_logs(directory, args.seconds)
            else:
                client = KaggleProbeClient(sdk_api(job["reference"].split("/")[0]))
                if args.action == "submit":
                    result = client.submit(directory)
                elif args.action == "reconcile":
                    result = client.reconcile(directory)
                elif args.action == "status":
                    result = client.status(directory)
                elif args.action == "fetch":
                    result = {"output_directory": str(client.fetch(directory)), "not_deserialized": True}
                elif args.action == "cancel":
                    result = client.cancel(directory, args.session_id, args.confirm_reference)
                elif args.action == "cooperative-cancel":
                    result = client.cooperative_cancel(directory)
                else:
                    result = client.confirm_cancel(directory)
        output(result)
        return 0
    except KeyboardInterrupt:
        output({"interrupted": True, "cancel_confirmed": False,
                "message": "The local command stopped. A submitted Kaggle job may still be running."})
        return 130
    except ProbeError as error:
        output({"error": str(error), "production_ready": False})
        return 2
    except (Exception, SystemExit) as error:
        output({"error": "Probe operation failed. Check network, account permissions and the selected job. Raw details omitted.",
                "error_type": type(error).__name__, "production_ready": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
