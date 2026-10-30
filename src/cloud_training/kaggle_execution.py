"""Per-attempt private Kaggle execution with durable, caller-owned journaling.

Every potentially ambiguous write is fenced BEFORE the network request. A
restart reconciles the same resource; it never submits a replacement fit.
"""
import copy
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlparse

from src.cloud_training.artifacts import MAX_BYTES, MAX_FILES, read_bundle, relative_path
from src.cloud_training.control import render_notebook, source_configuration, with_control
from src.cloud_training.kaggle_transport import ProviderError
from src.cloud_training.probe import canonical, sha256_file
from src.cloud_training.errors import validate_failure

TERMINAL = {"COMPLETE", "ERROR", "CANCEL_ACKNOWLEDGED"}


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


def download_file(url, path, *, limit, expected_size=None, expected_sha256=None, refresh_url=None):
    """Stream only provider-issued HTTPS URLs; bound bytes before disk writes."""
    import requests
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ProviderError("Output destination must not follow symlinks.", operation="output_download", category="unsafe_destination")
    if path.exists():
        raise ProviderError("Output destination already exists.", operation="output_download", category="destination_exists")
    for attempt in range(3):
        created, digest, size = False, hashlib.sha256(), 0
        if attempt and refresh_url:
            url = refresh_url()  # Fresh signed URL for the SAME version/file.
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ProviderError("Kaggle returned an invalid output download URL.", operation="output_download", category="invalid_url")
        try:
            with requests.get(url, stream=True, timeout=(10, 45)) as response:
                response.raise_for_status()
                if response.headers.get("Content-Length") and int(response.headers["Content-Length"]) > limit:
                    raise ProviderError("Output download exceeds its byte limit.", operation="output_download", category="byte_budget")
                with path.open("xb") as stream:
                    created = True
                    for chunk in response.iter_content(128 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise ProviderError("Output download exceeds its byte limit.", operation="output_download", category="byte_budget")
                        digest.update(chunk)
                        stream.write(chunk)
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
                 sleep=time.sleep, downloader=download_file):
        self.transport, self.directory = transport, Path(directory)
        self.ledger, self.save_callback = copy.deepcopy(ledger), save
        self.clock, self.sleep, self.downloader = clock, sleep, downloader
        self.stream = None
        self.last_sequence = 0

    def save(self, **values):
        self.ledger.update(values)
        self.save_callback(copy.deepcopy(self.ledger))

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
        inputs = self.directory / "input"
        metadata = {"id": self.ledger["dataset_reference"], "title": self.ledger["dataset_reference"].split("/")[1],
                    "licenses": [{"name": "other"}], "description": "Private temporary training snapshot. All rights reserved."}
        (inputs / "dataset-metadata.json").write_bytes(canonical(metadata))
        if not self.ledger["dataset_pending"]:
            self.save(dataset_pending=True)
            self.transport.call("dataset_create", directory=str(inputs), timeout=300)
        deadline = self.clock() + 600
        while True:
            if cancelled():
                raise TrainingCancelled("The run was cancelled before notebook submission.")
            try:
                info = self.transport.call("dataset_check", reference=self.ledger["dataset_reference"])
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
            response = self.transport.call("kernel_push", directory=str(notebook), no_run=False, timeout=90)
            if response.get("rejected"):
                self.save(submit_rejected=True, expected_version=response.get("version"))
                raise ProviderError("The provider rejected this notebook execution.")
            self.save(expected_version=response["version"])
        # An uncertain response is recovered by read-back, never by a new push.
        remote = self.transport.call("kernel_get", reference=self.ledger["reference"])
        self.verify_remote(remote)
        version = remote["current_version_number"]
        if (type(version) is not int or version < 1
                or (self.ledger.get("expected_version") is not None and version != self.ledger["expected_version"])):
            raise ProviderError("The notebook execution version could not be reconciled.")
        self.save(version=version, submit_pending=False)
        return self.reference

    def control(self, *, cancel=False):
        cancel = bool(cancel or self.ledger.get("cancel_requested"))
        remote = self.transport.call("kernel_get", reference=self.ledger["reference"])
        source = self.verify_remote(remote)
        controlled = with_control(source, cancel=cancel, clock=self.clock())
        directory = self.directory / "control"
        directory.mkdir(exist_ok=True)
        (directory / "kernel-metadata.json").write_bytes(canonical(self.metadata()))
        (directory / "train.py").write_text(controlled, encoding="utf-8")
        self.save(control_pending=True, cancel_requested=self.ledger.get("cancel_requested", False) or cancel)
        # Quick Save edits the existing private source. It never starts a fit.
        try:
            response = self.transport.call("kernel_push", directory=str(directory), no_run=True, timeout=75)
            if response.get("rejected"):
                raise ProviderError("The provider did not accept the control update.", operation="kernel_push", category="control_rejected")
        except ProviderError as error:
            # A control response can be lost after Quick Save was accepted.
            # Reconcile exact source; never repeat this write or start a fit.
            if error.category == "control_rejected":
                raise
            remote = self.transport.call("kernel_get", reference=self.ledger["reference"])
            self.verify_remote(remote)
            if remote["source"].replace("\r\n", "\n") != controlled:
                raise error
        else:
            remote = self.transport.call("kernel_get", reference=self.ledger["reference"])
        self.verify_remote(remote)
        if remote["source"].replace("\r\n", "\n") != controlled:
            raise ProviderError("The cooperative control write was not confirmed.")
        self.save(control_pending=False, lease_until=self.clock() + 180,
                  confirmed_control_writes=self.ledger.get("confirmed_control_writes", 0) + 1)

    def status(self):
        state = self.transport.call("status", reference=self.reference)["state"]
        if state in TERMINAL:
            self.save(terminal=state, remote_stopped=True)
        return state

    def drain(self, progress):
        if self.stream is None:
            self.stream = self.transport.stream(self.reference, self.ledger["snapshot"]["context"])
        for event in self.stream.drain():
            if "control" in event:
                stage = event["control"]
                if stage not in {"control_ready", "child_running", "child_completed", "cancelled", "failed"}:
                    raise ProviderError("Invalid notebook control event.")
                self.save(**{stage + "_observed": True})
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
        if self.stream.reader_failed:
            raise ProviderError("Notebook progress stream could not be validated.")
        if self.stream.returncode() is not None:
            self.stream.stop()
            self.stream = None

    def run(self, progress, cancelled):
        deadline, renewal = self.clock() + 8 * 3600, 0
        try:
            self.submit(cancelled)
            while self.clock() < deadline:
                if cancelled():
                    raise TrainingCancelled("The training run requested cancellation.")
                if self.clock() >= renewal:
                    self.control()
                    renewal = self.clock() + 45
                self.drain(progress)
                state = self.status()
                if state in TERMINAL:
                    # Final persisted stream is replayed to collect last events.
                    for _ in range(3):
                        self.drain(progress)
                        if self.ledger.get("child_completed_observed"):
                            break
                        self.sleep(1)
                    if state != "COMPLETE" or not self.ledger.get("child_completed_observed"):
                        category = ("ml_execution_failed" if self.ledger.get("failure_diagnostic") or self.ledger.get("supervisor_child_returncode") not in (None, 0) else
                                    "supervisor_failed" if self.ledger.get("supervisor_failure") else "execution_validation")
                        raise ProviderError("The notebook ended without successful ML output.", category=category)
                    return self.fetch()
                self.sleep(3)
            raise ProviderError("Kaggle execution exceeded its monitoring deadline.")
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
            if self.ledger.get("submit_rejected") and not self.transport.call(
                    "resource_exists", kind="notebook", reference=self.ledger["reference"])["exists"]:
                self.save(remote_stopped=True, notebook_cleaned=True)
                return True
            remote = self.transport.call("kernel_get", reference=self.ledger["reference"])
            self.verify_remote(remote)
            self.save(version=remote["current_version_number"])
        if self.status() not in TERMINAL:
            self.control(cancel=True)
            # The owning watcher retries until the exact original version is
            # terminal. Expiry is additional protection, never a fake stop ack.
            return False
        return True

    def fetch(self):
        if not self.ledger.get("remote_stopped") or self.ledger.get("terminal") != "COMPLETE":
            raise ProviderError("Only completed output may be received.")
        self.save(phase="output_listing")
        files = self.transport.call("output_list", reference=self.reference)["files"]
        def listed_url(name):
            nonlocal files
            for retry in range(12):
                if name in files:
                    return files[name]
                if retry < 11:
                    self.sleep(5)
                    files = self.transport.call("output_list", reference=self.reference)["files"]
            raise ProviderError("Notebook output file is not ready.", operation="output_listing", category="file_not_ready")

        def refreshed_url(name):
            nonlocal files
            files = self.transport.call("output_list", reference=self.reference)["files"]
            return listed_url(name)
        manifest_name = "output/manifest.json"
        manifest_url = listed_url(manifest_name)
        output = self.directory / "output"
        output.mkdir(exist_ok=False)
        self.save(phase="output_download")
        self.downloader(manifest_url, output / "manifest.json", limit=1024 * 1024,
                        refresh_url=lambda: refreshed_url(manifest_name))
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
            if (name in names or type(item["size"]) is not int or item["size"] <= 0
                    or not isinstance(item["sha256"], str) or not re.fullmatch("[a-f0-9]{64}", item["sha256"])):
                raise ProviderError("Invalid output path, size or checksum.", operation="output_manifest", category="invalid_descriptor")
            names.add(name)
            total += item["size"]
        if total > MAX_BYTES:
            raise ProviderError("Output bytes exceed their budget.", operation="output_manifest", category="byte_budget")
        for item in entries:
            remote = "output/" + item["path"]
            url = listed_url(remote)
            self.downloader(url, output / item["path"], limit=item["size"], expected_size=item["size"],
                            expected_sha256=item["sha256"], refresh_url=lambda name=remote: refreshed_url(name))
        result = read_bundle(output, self.ledger["snapshot"]["context"])
        self.save(phase="output_verified")
        return result

    def cleanup(self):
        if not self.ledger.get("remote_stopped"):
            return False
        # Read identity before each destructive provider operation. References
        # were derived from a unique attempt UUID and never supplied by a user.
        if self.ledger.get("version") and not self.ledger["notebook_cleaned"]:
            if self.ledger.get("notebook_delete_pending") and not self.transport.call(
                    "resource_exists", kind="notebook", reference=self.ledger["reference"])["exists"]:
                self.save(notebook_cleaned=True)
            else:
                remote = self.transport.call("kernel_get", reference=self.ledger["reference"])
                self.verify_remote(remote)
                self.save(notebook_delete_pending=True)
                self.transport.call("cleanup", notebook=self.ledger["reference"])
                if self.transport.call("resource_exists", kind="notebook", reference=self.ledger["reference"])["exists"]:
                    return False
                self.save(notebook_cleaned=True)
        if self.ledger["dataset_pending"] and not self.ledger["dataset_cleaned"]:
            if not self.transport.call(
                    "resource_exists", kind="dataset", reference=self.ledger["dataset_reference"])["exists"]:
                self.save(dataset_cleaned=True)
            else:
                self.transport.call("dataset_check", reference=self.ledger["dataset_reference"])
                self.save(dataset_delete_pending=True)
                self.transport.call("cleanup", dataset=self.ledger["dataset_reference"])
                if self.transport.call("resource_exists", kind="dataset", reference=self.ledger["dataset_reference"])["exists"]:
                    return False
                self.save(dataset_cleaned=True)
        return True
