"""Per-attempt private Kaggle execution with durable, caller-owned journaling.

Every potentially ambiguous write is fenced BEFORE the network request. A
restart reconciles the same resource; it never submits a replacement fit.
"""
import copy
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import time
import threading
from urllib.parse import urlparse

from src.cloud_training.artifacts import MAX_BYTES, MAX_FILES, read_bundle, relative_path
from src.cloud_training.control import render_notebook, source_configuration, source_control, with_control
from src.cloud_training.kaggle_transport import ProviderError
from src.cloud_training.probe import canonical, sha256_file
from src.cloud_training.errors import validate_failure
from src.cloud_training.telemetry import operation_timing, source_timing

TERMINAL = {"COMPLETE", "ERROR", "CANCEL_ACKNOWLEDGED"}
TERMINAL_EVENT_WAIT_SECONDS = 90


class TrainingCancelled(ProviderError):
    pass


def initial_ledger(snapshot, username):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{3,50}", username):
        raise ValueError("Invalid Kaggle username.")
    suffix = snapshot["context"]["attempt_id"].replace("-", "")
    reference, dataset_reference = username + "/traffic-train-" + suffix, username + "/traffic-input-" + suffix
    supervisor_configuration = source_configuration(render_notebook(snapshot, reference, dataset_reference))
    return {"backend": "kaggle", "snapshot": copy.deepcopy(snapshot),
            "supervisor_configuration": supervisor_configuration,
            "reference": username + "/traffic-train-" + suffix,
            "dataset_reference": username + "/traffic-input-" + suffix,
            "dataset_pending": False, "submit_pending": False, "version": None,
            "terminal": None, "remote_stopped": False,
            "notebook_cleaned": False, "dataset_cleaned": False}


def download_file(url, path, *, limit, expected_size=None, expected_sha256=None, refresh_url=None,
                  session=None, cancelled=lambda: False):
    """Stream only provider-issued HTTPS URLs; bound bytes before disk writes."""
    import requests
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ProviderError("Output destination must not follow symlinks.", operation="output_download", category="unsafe_destination")
    if path.exists():
        raise ProviderError("Output destination already exists.", operation="output_download", category="destination_exists")
    for attempt in range(3):
        if cancelled():
            raise TrainingCancelled("Output reception was cancelled.")
        created, digest, size = False, hashlib.sha256(), 0
        if attempt and refresh_url:
            url = refresh_url()  # Fresh signed URL for the SAME version/file.
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ProviderError("Kaggle returned an invalid output download URL.", operation="output_download", category="invalid_url")
        try:
            with (session.get if session is not None else requests.get)(url, stream=True, timeout=(10, 45)) as response:
                response.raise_for_status()
                if response.headers.get("Content-Length") and int(response.headers["Content-Length"]) > limit:
                    raise ProviderError("Output download exceeds its byte limit.", operation="output_download", category="byte_budget")
                with path.open("xb") as stream:
                    created = True
                    for chunk in response.iter_content(128 * 1024):
                        if cancelled():
                            raise TrainingCancelled("Output reception was cancelled.")
                        size += len(chunk)
                        if size > limit:
                            raise ProviderError("Output download exceeds its byte limit.", operation="output_download", category="byte_budget")
                        digest.update(chunk)
                        stream.write(chunk)
            if cancelled():
                raise TrainingCancelled("Output reception was cancelled.")
            if expected_size is not None and (size != expected_size or digest.hexdigest() != expected_sha256):
                raise ProviderError("Downloaded output differs from its manifest.", operation="output_download", category="checksum_mismatch")
            return
        except Exception as error:
            if created and path.is_file() and not path.is_symlink():
                path.unlink()  # Only this call's incomplete file.
            if isinstance(error, ProviderError):
                raise
            status = getattr(getattr(error, "response", None), "status_code", None)
            transient = (isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                                           requests.exceptions.ChunkedEncodingError))
                         or status in {401, 403, 404, 408, 429, 500, 502, 503, 504})
            if transient and attempt < 2:
                time.sleep(attempt + 1)
                continue  # GET only; no upload, fit or receipt is repeated.
            category = "response_timeout" if isinstance(error, requests.exceptions.Timeout) else "http_error" if status else "transfer_error"
            raise ProviderError("Output could not be downloaded and verified.", status,
                                operation="output_download", category=category) from None


class KaggleExecution:
    def __init__(self, transport, directory, ledger, save, *, clock=time.time,
                 sleep=time.sleep, downloader=download_file, monotonic=time.monotonic, background_progress=True):
        self.transport, self.directory = transport, Path(directory)
        self.ledger, self.save_callback = copy.deepcopy(ledger), save
        self.clock, self.sleep, self.downloader = clock, sleep, downloader
        self.stream = None
        self.last_sequence = 0
        self.monotonic = monotonic
        self._journal_lock = threading.RLock()
        self._drain_lock = threading.Lock()
        self._progress_stop = threading.Event()
        self._progress_thread = None
        self._progress_error = None
        self._next_stream_at = 0
        self.background_progress = background_progress

    def timestamp(self):
        return datetime.fromtimestamp(self.clock(), UTC).isoformat()

    @contextmanager
    def measured(self, operation, *, byte_count=0):
        start, failed = self.monotonic(), False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            with self._journal_lock:
                telemetry = copy.deepcopy(self.ledger.get("telemetry", {}))
                operation_timing(telemetry, operation, max(0, self.monotonic() - start),
                                 failed=failed, byte_count=byte_count)
                self.save(telemetry=telemetry)

    def call(self, operation, **values):
        byte_count = (sum(item["size"] for item in self.ledger["snapshot"]["files"].values())
                      if operation == "dataset_create" else 0)
        with self.measured(operation, byte_count=byte_count):
            result = self.transport.call(operation, **values)
            components = result.pop("_rpc_timing", None)
            reused = result.pop("_rpc_session_reused", None)
            if components or reused is not None:
                with self._journal_lock:
                    telemetry = copy.deepcopy(self.ledger.get("telemetry", {}))
                    for name, seconds in (components or {}).items():
                        operation_timing(telemetry, name, seconds)
                    if reused is not None:
                        operation_timing(telemetry, "sdk_session_reuse" if reused else "sdk_session_start", 0)
                    self.save(telemetry=telemetry)
            return result

    def milestone(self, name, event=None):
        with self._journal_lock:
            telemetry = copy.deepcopy(self.ledger.get("telemetry", {}))
            markers = telemetry.setdefault("milestones", {})
            if name not in markers:
                if len(markers) >= 40:
                    raise ProviderError("Timing milestone budget exceeded.")
                markers[name] = {"observed_at": self.timestamp(), **source_timing(event or {})}
                self.save(telemetry=telemetry)

    def phase(self, name):
        if self.ledger.get("phase") != name:
            self.save(phase=name)
            self.milestone(name)

    def save(self, **values):
        with self._journal_lock:
            self.ledger.update(values)
            self.save_callback(copy.deepcopy(self.ledger))

    def _start_progress(self, progress):
        if not self.background_progress:
            return
        self._progress_stop.clear()
        def pump():
            try:
                while not self._progress_stop.is_set():
                    self.drain(progress)
                    self._progress_stop.wait(.25)
            except Exception as error:
                self._progress_error = error
        self._progress_thread = threading.Thread(target=pump, name="kaggle-progress", daemon=True)
        self._progress_thread.start()

    def _stop_progress(self):
        self._progress_stop.set()
        if self._progress_thread:
            self._progress_thread.join(timeout=5)
            if self._progress_thread.is_alive():
                raise ProviderError("Progress delivery did not stop within its deadline.", category="progress_timeout")
        if self._progress_error:
            raise self._progress_error

    @property
    def reference(self):
        version = self.ledger.get("version")
        if type(version) is not int or version < 1:
            raise ProviderError("No exact notebook execution version was confirmed.")
        return self.ledger["reference"] + "/" + str(version)

    def metadata(self):
        context = self.ledger["snapshot"]["context"]
        ref = self.ledger["reference"]
        return {"id": ref, "title": ref.split("/")[1], "code_file": "train.py",
                "language": "python", "kernel_type": "script", "is_private": True,
                "enable_gpu": context["branch"] != "tree", "enable_tpu": False, "enable_internet": True,
                "dataset_sources": [self.ledger["dataset_reference"] + "/1"],
                "competition_sources": [], "kernel_sources": [], "model_sources": []}

    def verify_remote(self, remote):
        snapshot = self.ledger["snapshot"]
        source = remote["source"].replace("\r\n", "\n")
        config = source_configuration(source)
        expected = self.ledger.get("supervisor_configuration") or source_configuration(
            render_notebook(snapshot, self.ledger["reference"], self.ledger["dataset_reference"], clock=self.clock()))
        sources = remote["dataset_data_sources"] or []
        if (config != expected or remote["ref"] != self.ledger["reference"] or remote["is_private"] is not True
                or remote["enable_gpu"] is not (snapshot["context"]["branch"] != "tree")
                or remote["enable_internet"] is not True or len(sources) != 1
                or sources[0] not in {self.ledger["dataset_reference"], self.ledger["dataset_reference"] + "/1"}
                or any(remote[key] for key in ("kernel_data_sources", "competition_data_sources", "model_data_sources"))):
            raise ProviderError("Remote notebook source, inputs, privacy or resources differ from this attempt.")
        return source

    def submit(self, cancelled=lambda: False):
        self.phase("input_upload")
        inputs = self.directory / "input"
        metadata = {"id": self.ledger["dataset_reference"], "title": self.ledger["dataset_reference"].split("/")[1],
                    "licenses": [{"name": "other"}], "description": "Private temporary training snapshot. All rights reserved."}
        (inputs / "dataset-metadata.json").write_bytes(canonical(metadata))
        if not self.ledger["dataset_pending"]:
            self.save(dataset_pending=True)
            self.call("dataset_create", directory=str(inputs), timeout=300)
        self.phase("input_processing")
        deadline = self.clock() + 600
        while True:
            if cancelled():
                raise TrainingCancelled("The run was cancelled before notebook submission.")
            try:
                info = self.call("dataset_check", reference=self.ledger["dataset_reference"])
            except ProviderError as error:
                if error.http_status not in {403, 404} or self.clock() >= deadline:
                    raise
                self.sleep(5)
                continue
            if info.get("private") is not True or info.get("version") != 1:
                raise ProviderError("The immutable private dataset version was not confirmed.")
            if info["status"] == "ready":
                break
            if info["status"] in {"error", "failed"} or self.clock() >= deadline:
                raise ProviderError("The private dataset did not become ready within its deadline.")
            self.sleep(5)
        self.milestone("input_ready")
        self.phase("notebook_submit")
        notebook = self.directory / "notebook"
        notebook.mkdir(exist_ok=True)
        (notebook / "kernel-metadata.json").write_bytes(canonical(self.metadata()))
        if self.ledger.get("version"):
            return self.reference
        if not self.ledger["submit_pending"]:
            if cancelled():
                raise TrainingCancelled("The run was cancelled before notebook submission.")
            source = render_notebook(self.ledger["snapshot"], self.ledger["reference"], self.ledger["dataset_reference"], clock=self.clock())
            (notebook / "train.py").write_text(source, encoding="utf-8")
            self.save(submit_pending=True)
            response = self.call("kernel_push", directory=str(notebook), no_run=False, timeout=90)
            if response.get("rejected"):
                self.save(submit_rejected=True, expected_version=response.get("version"))
                raise ProviderError("The provider rejected this notebook execution.", operation="kernel_push",
                                    category="submit_rejected", diagnostic=response.get("diagnostic"))
            self.save(expected_version=response["version"])
        # An uncertain response is recovered by read-back, never by a new push.
        remote = self.call("kernel_get", reference=self.ledger["reference"])
        self.verify_remote(remote)
        version = remote["current_version_number"]
        if (type(version) is not int or version < 1
                or (self.ledger.get("expected_version") is not None and version != self.ledger["expected_version"])):
            raise ProviderError("The notebook execution version could not be reconciled.")
        self.save(version=version, submit_pending=False)
        self.milestone("submit_confirmed")
        self.phase("provider_wait")
        return self.reference

    def control(self, *, cancel=False):
        cancel = bool(cancel or self.ledger.get("cancel_requested"))
        remote = self.call("kernel_get", reference=self.ledger["reference"])
        source = self.verify_remote(remote)
        cancel = bool(cancel or source_control(source)["cancel"])
        base_version = remote["current_version_number"]
        if type(base_version) is not int or base_version < self.ledger.get("control_version", self.ledger["version"]):
            raise ProviderError("Control version identity differs.", operation="kernel_get", category="control_identity")
        pending = self.ledger.get("pending_control") if self.ledger.get("control_pending") else None
        if pending:
            # A previous process may have lost the reply. Read back its original
            # intent, never replace it with a newly generated lease or write.
            controlled = with_control(source, cancel=pending["cancel"], clock=pending["lease_until"] - 180)
            if (hashlib.sha256(controlled.encode()).hexdigest() != pending["sha256"]
                    or source != controlled or base_version <= pending["base_version"]):
                raise ProviderError("Pending control update is not confirmed.", operation="kernel_get",
                                    category="control_unconfirmed")
            self.save(control_pending=False, pending_control=None, control_version=base_version,
                      lease_until=pending["lease_until"], last_control_confirmed_at=self.timestamp(),
                      confirmed_control_writes=self.ledger.get("confirmed_control_writes", 0) + 1)
            if not cancel or pending["cancel"]:
                return
        control_clock = self.clock()
        controlled = with_control(source, cancel=cancel, clock=control_clock)
        directory = self.directory / "control"
        directory.mkdir(exist_ok=True)
        (directory / "kernel-metadata.json").write_bytes(canonical(self.metadata()))
        (directory / "train.py").write_text(controlled, encoding="utf-8")
        pending = {"base_version": base_version, "sha256": hashlib.sha256(controlled.encode()).hexdigest(),
                   "lease_until": control_clock + 180, "cancel": cancel, "writes": 0}
        self.save(control_pending=True, pending_control=pending,
                  cancel_requested=self.ledger.get("cancel_requested", False) or cancel)
        # Quick Save edits the existing private source. It never starts a fit.
        for attempt in range(2):
            pending["writes"] += 1
            self.save(pending_control=copy.deepcopy(pending))
            error, response = None, None
            try:
                response = self.call("kernel_push", directory=str(directory), no_run=True, timeout=75)
                if response.get("rejected"):
                    raise ProviderError("The provider did not accept the control update.", operation="kernel_push",
                                        category="control_rejected", diagnostic=response.get("diagnostic"))
            except ProviderError as failure:
                error = failure
                diagnostic = failure.diagnostic or {}
                self.save(last_failure={"operation": failure.operation, "category": failure.category,
                                        "http_status": failure.http_status, "diagnostic": diagnostic})
                if diagnostic.get("reason") in {"authentication", "quota", "invalid_sources"}:
                    self.save(control_pending=False, pending_control=None)
                    raise
            remote = self.call("kernel_get", reference=self.ledger["reference"])
            actual = self.verify_remote(remote)
            version = remote["current_version_number"]
            expected = response.get("version") if response and not response.get("rejected") else None
            if (type(version) is int and version > base_version and actual == controlled
                    and (expected is None or expected == version)):
                self.save(control_pending=False, pending_control=None, control_version=version,
                          lease_until=pending["lease_until"], last_control_confirmed_at=self.timestamp(),
                          confirmed_control_writes=self.ledger.get("confirmed_control_writes", 0) + 1)
                return
            # Only an explicit transient refusal AND unchanged exact source/
            # version permit one retry. A timeout may still commit: never retry it.
            retryable = (error is not None and error.category == "control_rejected"
                         and (error.diagnostic or {}).get("retryable") is True
                         and version == base_version and actual == source
                         and self.clock() < pending["lease_until"] - 75)
            if attempt == 0 and retryable:
                self.sleep(.5)
                continue
            if error:
                if error.category == "control_rejected" and version == base_version and actual == source:
                    self.save(control_pending=False, pending_control=None)
                raise error
            raise ProviderError("The cooperative control write was not confirmed.",
                                operation="kernel_get", category="control_unconfirmed")

    def status(self):
        state = self.call("status", reference=self.reference)["state"]
        changed = self.ledger.get("provider_state") != state
        checked_at = self.timestamp()
        self.save(provider_state=state, provider_checked_at=checked_at,
                  provider_observed_at=checked_at if changed else self.ledger.get("provider_observed_at"))
        if changed:
            self.milestone("provider_" + state.lower() + "_observed")
        if state in TERMINAL:
            self.save(terminal=state, remote_stopped=True)
        return state

    def drain(self, progress):
        with self._drain_lock:
            if self._progress_error:
                raise self._progress_error
            self._drain(progress)

    def _drain(self, progress):
        if self.stream is None:
            if self.ledger.get("child_completed_observed"):
                return
            if self.monotonic() < self._next_stream_at:
                return
            self.stream = self.transport.stream(self.reference, self.ledger["snapshot"]["context"])
        events = self.stream.drain()
        # returncode() joins the stdout reader after process exit. That reader
        # can enqueue the final acknowledgement AFTER the first queue drain.
        ended = self.stream.returncode() is not None
        if ended:
            events.extend(self.stream.drain())
        for event in events:
            if "control" in event:
                stage = event["control"]
                if stage not in {"supervisor_started", "control_ready", "input_ready", "runtime_preparing",
                                 "runtime_ready", "child_running", "child_completed", "output_writing",
                                 "output_written", "cancelled", "failed"}:
                    raise ProviderError("Invalid notebook control event.")
                new_stage = not self.ledger.get(stage + "_observed")
                self.milestone("supervisor_input_ready" if stage == "input_ready" else stage, event)
                self.save(**{stage + "_observed": True})
                if (new_stage and stage in {"supervisor_started", "runtime_preparing", "runtime_ready", "child_running", "output_writing"}
                        and (self.last_sequence == 0 or stage == "output_writing")):
                    self.phase(stage)
                if "failure" in event:
                    if stage != "failed":
                        raise ProviderError("Unexpected child failure stage.")
                    self.save(failure_diagnostic=validate_failure(event["failure"]))
                if "supervisor_failure" in event:
                    status, phase = event.get("supervisor_http_status"), event.get("supervisor_phase")
                    child_code = event.get("supervisor_child_returncode")
                    if (stage != "failed" or phase not in {"startup", "control_check", "input_validation", "child_monitoring"}
                            or status is not None and (type(status) is not int or not 100 <= status <= 599)):
                        raise ProviderError("Invalid supervisor failure metadata.")
                    if child_code is not None and (type(child_code) is not int or not -999 <= child_code <= 999):
                        raise ProviderError("Invalid child exit code.")
                    self.save(supervisor_failure=validate_failure(event["supervisor_failure"]),
                              supervisor_http_status=status, supervisor_phase=phase,
                              supervisor_child_returncode=child_code)
            else:
                sequence = event.get("sequence")
                if type(sequence) is not int or sequence < 1:
                    raise ProviderError("Invalid notebook progress sequence.")
                if sequence > self.last_sequence:
                    progress(event)
                    self.last_sequence = sequence
                    if self.ledger.get("phase") not in {"ml_running", "output_writing"}:
                        self.phase("ml_running")
        if self.stream.reader_failed:
            raise ProviderError("Notebook progress stream could not be validated.")
        if ended or self.ledger.get("child_completed_observed"):
            self.stream.stop()
            self.stream = None
            self._next_stream_at = self.monotonic() + 3

    def run(self, progress, cancelled):
        deadline, renewal = self.clock() + 8 * 3600, 0
        queued_polls = 0
        try:
            self.submit(cancelled)
            self._start_progress(progress)
            while self.clock() < deadline:
                if cancelled():
                    raise TrainingCancelled("The training run requested cancellation.")
                self.drain(progress)
                state = self.status()
                self.drain(progress)
                if state in TERMINAL:
                    # Provider status and persisted logs arrive independently.
                    # Re-read the SAME version while awaiting its completion
                    # acknowledgement; COMPLETE alone never permits output.
                    wait = TERMINAL_EVENT_WAIT_SECONDS if state == 'COMPLETE' else 3
                    event_deadline = min(deadline, self.clock() + wait)
                    for _ in range(wait + 1):
                        if cancelled():
                            raise TrainingCancelled("The training run requested cancellation.")
                        self.drain(progress)
                        if self.ledger.get("child_completed_observed") or self.ledger.get('failed_observed'):
                            break
                        remaining = event_deadline - self.clock()
                        if remaining <= 0:
                            break
                        self.sleep(min(1, remaining))
                    if state != "COMPLETE" or not self.ledger.get("child_completed_observed") or self.ledger.get('failed_observed'):
                        category = ("ml_execution_failed" if self.ledger.get("failure_diagnostic") or self.ledger.get("supervisor_child_returncode") not in (None, 0) else
                                    "supervisor_failed" if self.ledger.get("supervisor_failure") else
                                    "completion_unconfirmed" if state == 'COMPLETE' else "execution_validation")
                        raise ProviderError("The notebook ended without successful ML output.",
                                            operation='terminal_events', category=category)
                    self._stop_progress()
                    return self.fetch(cancelled=cancelled)
                if self.clock() >= renewal:
                    # Do not Quick Save an execution already observed terminal.
                    # Schedule from the actual lease written, not from reply time.
                    self.control()
                    renewal = min(self.clock() + 45, self.ledger.get("lease_until", self.clock() + 180) - 90)
                    self.drain(progress)
                    if renewal <= self.clock():
                        continue
                queued_polls = (queued_polls + 1 if state == "QUEUED" and self.last_sequence == 0
                                and not self.ledger.get("child_running_observed") else 0)
                delay = min(15, 3 * 2 ** min(queued_polls - 1, 3)) if queued_polls else 3
                before = (self.last_sequence, bool(self.ledger.get("child_running_observed")), bool(self.ledger.get("child_completed_observed")))
                # Poll Kaggle less during a confirmed queue. Cancellation,
                # incoming ML events and lease renewal remain responsive.
                for _ in range(delay):
                    self.sleep(1)
                    if cancelled():
                        raise TrainingCancelled("The training run requested cancellation.")
                    self.drain(progress)
                    after = (self.last_sequence, bool(self.ledger.get("child_running_observed")), bool(self.ledger.get("child_completed_observed")))
                    if after != before or self.clock() >= renewal:
                        break
            raise ProviderError("Kaggle execution exceeded its monitoring deadline.")
        finally:
            try:
                self._stop_progress()
            finally:
                if self.stream is not None:
                    self.stream.stop()  # Stops the log reader, never claims remote cancellation.
                    self.stream = None

    def stop(self):
        if self.ledger.get("remote_stopped"):
            return True
        if not self.ledger.get("submit_pending") and not self.ledger.get("version"):
            self.save(remote_stopped=True)
            return True
        if not self.ledger.get("version"):
            if self.ledger.get("submit_rejected") and not self.call(
                    "resource_exists", kind="notebook", reference=self.ledger["reference"])["exists"]:
                self.save(remote_stopped=True, notebook_cleaned=True)
                return True
            remote = self.call("kernel_get", reference=self.ledger["reference"])
            self.verify_remote(remote)
            self.save(version=remote["current_version_number"])
        if self.status() not in TERMINAL:
            self.control(cancel=True)
            # The owning watcher retries until the exact original version is
            # terminal. Expiry is additional protection, never a fake stop ack.
            return False
        return True

    def fetch(self, *, cancelled=lambda: False):
        if not self.ledger.get("remote_stopped") or self.ledger.get("terminal") != "COMPLETE":
            raise ProviderError("Only completed output may be received.")
        self.phase("output_listing")
        files = self.call("output_list", reference=self.reference)["files"]
        listing_lock = threading.RLock()
        stop_transfers = threading.Event()
        cancel_lock = threading.Lock()
        cancellation = {"checked_at": float("-inf"), "requested": False}
        def cancel_check():
            if stop_transfers.is_set():
                return True
            with cancel_lock:
                if self.monotonic() - cancellation["checked_at"] >= .5:
                    cancellation.update(checked_at=self.monotonic(), requested=bool(cancelled()))
                return cancellation["requested"]
        def listed_url(name):
            nonlocal files
            for retry in range(12):
                if cancel_check():
                    raise TrainingCancelled("Output listing was cancelled.")
                if name in files:
                    return files[name]
                if retry < 11:
                    self.sleep(5)
                    files = self.call("output_list", reference=self.reference)["files"]
            raise ProviderError("Notebook output file is not ready.", operation="output_listing", category="file_not_ready")

        def refreshed_url(name):
            nonlocal files
            with listing_lock:
                files = self.call("output_list", reference=self.reference)["files"]
                return listed_url(name)
        manifest_name = "output/manifest.json"
        manifest_url = listed_url(manifest_name)
        output = self.directory / "output"
        output.mkdir(exist_ok=False)
        self.phase("output_download")
        with self.measured("manifest_download"):
            self.downloader(manifest_url, output / "manifest.json", limit=1024 * 1024,
                            refresh_url=lambda: refreshed_url(manifest_name), cancelled=cancel_check)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("context") != self.ledger["snapshot"]["context"] or not isinstance(manifest.get("files"), list):
            raise ProviderError("Output manifest belongs to another attempt.", operation="output_manifest", category="context_mismatch")
        entries, total, names = manifest["files"], 0, set()
        if len(entries) > MAX_FILES:
            raise ProviderError("Output file count exceeds its budget.", operation="output_manifest", category="file_budget")
        for item in entries:
            if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
                raise ProviderError("Invalid output descriptor.", operation="output_manifest", category="invalid_descriptor")
            name = relative_path(item["path"])
            if (name in names or name == "manifest.json" or name.startswith("manifest.json/")
                    or any(name.startswith(other + "/") or other.startswith(name + "/") for other in names)
                    or type(item["size"]) is not int or item["size"] <= 0
                    or not isinstance(item["sha256"], str) or not re.fullmatch("[a-f0-9]{64}", item["sha256"])):
                raise ProviderError("Invalid output path, size or checksum.", operation="output_manifest", category="invalid_descriptor")
            names.add(name)
            total += item["size"]
        if total > MAX_BYTES:
            raise ProviderError("Output bytes exceed their budget.", operation="output_manifest", category="byte_budget")
        # Resolve the immutable manifest's paths first. A bounded pool then
        # reuses one HTTP session per thread, never a shared mutable Session.
        downloads = [(item, listed_url("output/" + item["path"])) for item in entries]
        sessions, local, sessions_lock = [], threading.local(), threading.Lock()
        def receive(item, url):
            import requests
            if not hasattr(local, "session"):
                local.session = requests.Session()
                with sessions_lock:
                    sessions.append(local.session)
            if cancel_check():
                raise TrainingCancelled("Output reception was cancelled.")
            remote = "output/" + item["path"]
            with self.measured("output_download", byte_count=item["size"]):
                self.downloader(url, output / item["path"], limit=item["size"], expected_size=item["size"],
                                expected_sha256=item["sha256"], refresh_url=lambda: refreshed_url(remote),
                                session=local.session, cancelled=cancel_check)
        try:
            with self.measured("output_transfer_wall"), ThreadPoolExecutor(max_workers=3, thread_name_prefix="kaggle-output") as pool:
                futures = [pool.submit(receive, item, url) for item, url in downloads]
                try:
                    for future in as_completed(futures):
                        future.result()
                except Exception:
                    stop_transfers.set()
                    for future in futures:
                        future.cancel()
                    raise
        finally:
            for session in sessions:
                session.close()
        if cancel_check():
            raise TrainingCancelled("Output verification was cancelled.")
        self.phase("output_verification")
        with self.measured("output_verification"):
            result = read_bundle(output, self.ledger["snapshot"]["context"])
        self.phase("output_verified")
        return result

    def cleanup(self):
        if not self.ledger.get("remote_stopped"):
            return False
        self.milestone("cleanup_started")
        # Read identity before each destructive provider operation. References
        # were derived from a unique attempt UUID and never supplied by a user.
        if self.ledger.get("version") and not self.ledger["notebook_cleaned"]:
            if self.ledger.get("notebook_delete_pending") and not self.call(
                    "resource_exists", kind="notebook", reference=self.ledger["reference"])["exists"]:
                self.save(notebook_cleaned=True)
            else:
                remote = self.call("kernel_get", reference=self.ledger["reference"])
                self.verify_remote(remote)
                self.save(notebook_delete_pending=True)
                self.call("cleanup", notebook=self.ledger["reference"])
                if self.call("resource_exists", kind="notebook", reference=self.ledger["reference"])["exists"]:
                    return False
                self.save(notebook_cleaned=True)
        if self.ledger["dataset_pending"] and not self.ledger["dataset_cleaned"]:
            if not self.call(
                    "resource_exists", kind="dataset", reference=self.ledger["dataset_reference"])["exists"]:
                self.save(dataset_cleaned=True)
            else:
                self.call("dataset_check", reference=self.ledger["dataset_reference"])
                self.save(dataset_delete_pending=True)
                self.call("cleanup", dataset=self.ledger["dataset_reference"])
                if self.call("resource_exists", kind="dataset", reference=self.ledger["dataset_reference"])["exists"]:
                    return False
                self.save(dataset_cleaned=True)
        self.milestone("cleanup_confirmed")
        return True
