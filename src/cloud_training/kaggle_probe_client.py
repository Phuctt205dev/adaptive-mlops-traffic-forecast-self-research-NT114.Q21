"""Kaggle SDK adapter for explicit, bounded capability probes only."""
import os
import json
from urllib.parse import parse_qs, urlparse

from src.cloud_training.probe import (
    ProbeError, load_job, parse_event, safe_status, separate_sdk_log, update_job, versioned_reference, write_json,
)


class KaggleProbeClient:
    def __init__(self, api, kernel_request_factory=None):
        self.api = api
        self.kernel_request_factory = kernel_request_factory

    @staticmethod
    def response_session_id(result, reference):
        """Only accept a session ID explicitly linked by the provider itself.

        No substring extraction from notebook IDs, error messages or output.
        """
        url = urlparse(getattr(result, "url", "") or "")
        if (url.scheme != "https" or url.netloc != "www.kaggle.com"
                or url.path.rstrip("/") != "/code/" + reference):
            return None
        values = parse_qs(url.query).get("kernelSessionId", [])
        if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
            return None
        value = int(values[0])
        return value if value > 0 else None

    def submit(self, directory):
        directory, job = load_job(directory)
        if job.get("submitted") or job.get("submission_pending"):
            raise ProbeError("This probe was already submitted or its submission is uncertain. Do not submit twice.")
        try:
            lease = os.open(directory / "submission.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise ProbeError("This probe already has a submission owner. Do not remove the lock or submit twice.") from None
        with os.fdopen(lease, "w", encoding="utf-8") as stream:
            stream.write(job["job_id"])
        # Fence retries before any network call. A lost response is not proof
        # that Kaggle did not create a version.
        job = update_job(directory, {"submission_pending": True})
        result = self.api.kernels_push(str(directory), timeout="240")
        job["submission_response"] = {
            "has_error": bool(getattr(result, "error", "")),
            "reference_matches": getattr(result, "ref", None) == job["reference"],
            "has_reference": bool(getattr(result, "ref", None)),
            "has_positive_version": type(getattr(result, "version_number", None)) is int and result.version_number > 0,
            "provider_link_has_session_id": self.response_session_id(result, job["reference"]) is not None,
        }
        job = update_job(directory, {"submission_response": job["submission_response"]})
        if (getattr(result, "error", "")
                or type(getattr(result, "version_number", None)) is not int or result.version_number < 1
                or any(getattr(result, name, None) for name in (
                    "invalid_tags", "invalid_dataset_sources", "invalid_competition_sources",
                    "invalid_kernel_sources", "invalid_model_sources"))):
            raise ProbeError("Kaggle did not confirm the prepared private notebook version. Submission remains fenced.")
        job["submission_expected_version"] = result.version_number
        candidate = self.response_session_id(result, job["reference"])
        if candidate is not None:
            job["provider_linked_session_candidate"] = candidate
        job = update_job(directory, {key: job[key] for key in ("submission_expected_version", "provider_linked_session_candidate") if key in job})
        # SaveKernel.ref is not necessarily owner/slug on the live service.
        # Confirm actual remote identity/source/privacy instead of accepting a
        # guessed response format or pushing a replacement version.
        return self.reconcile(directory)

    def reconcile(self, directory, request_factory=None):
        """Recover an uncertain submit by reading exact remote code/privacy.

        Never push another version and never accept a slug alone as proof.
        """
        directory, job = load_job(directory)
        if job.get("submitted") or job.get("submission_pending") is not True:
            raise ProbeError("Only a pending, uncertain submission can be reconciled.")
        request_factory = request_factory or self.kernel_request_factory
        if request_factory is None:
            from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest
            request_factory = ApiGetKernelRequest
        request = request_factory()
        request.user_name, request.kernel_slug = job["reference"].split("/")
        with self.api.build_kaggle_client() as client:
            response = client.kernels.kernels_api_client.get_kernel(request)
        metadata, blob = response.metadata, response.blob
        local_source = (directory / "probe.py").read_text(encoding="utf-8").replace("\r\n", "\n")
        remote_source = blob.source.replace("\r\n", "\n")
        version = metadata.current_version_number
        if (metadata.ref != job["reference"] or metadata.is_private is not True
                or type(version) is not int or version < 1 or remote_source != local_source
                or (job.get("submission_expected_version") is not None and version != job["submission_expected_version"])
                or metadata.enable_gpu is not (job["accelerator"] == "gpu")
                or metadata.enable_internet is not (job["purpose"] == "cooperative-cancel")
                or metadata.kernel_type != "script" or metadata.language.lower() != "python"
                or any(getattr(metadata, name, None) for name in (
                    "dataset_data_sources", "kernel_data_sources", "competition_data_sources", "model_data_sources"))):
            raise ProbeError("Remote notebook source, privacy, resources or inputs differ; submission remains fenced.")
        changes = dict(submitted=True, submission_pending=False, version=version,
                       kernel_id=metadata.id, submission_reconciled=True)
        candidate = job.get("provider_linked_session_candidate")
        if type(candidate) is int and candidate > 0 and candidate != metadata.id:
            changes.update(session_id=candidate, session_identity_verified=True,
                           session_identity_source="save_kernel_response_url")
        job = update_job(directory, changes)
        return {"reference": versioned_reference(job), "submitted": True, "reconciled": True,
                "source_and_privacy_verified": True,
                "automatic_cancellation_ready": bool(job.get("session_identity_verified"))}

    def status(self, directory):
        directory, job = load_job(directory)
        response = self.api.kernels_status(versioned_reference(job))
        state = safe_status(response.status)
        job = update_job(directory, {"last_state": state})
        return {"job_id": job["job_id"], "reference": versioned_reference(job), "state": state,
                "has_failure_message": bool(getattr(response, "failure_message", "")),
                "production_ready": False}

    def events(self, directory):
        _, job = load_job(directory)
        # Official SDK stream, pinned to the exact submitted version. It must
        # run in a caller-owned subprocess with a wall-time limit.
        for item in self.api.kernels_logs_stream(versioned_reference(job)):
            raw = item.get("data", "") if isinstance(item, dict) else ""
            for line in raw.splitlines():
                event = parse_event(line, job)
                if event is not None:
                    yield event

    def fetch(self, directory):
        directory, job = load_job(directory)
        state = self.status(directory)["state"]
        if state != "COMPLETE" or job["purpose"] != "smoke":
            raise ProbeError("Only a completed synthetic smoke probe can be downloaded.")
        output = directory / "output"
        output.mkdir(exist_ok=False)
        # Exact flat names only. Never download all notebook output, logs,
        # arbitrary relative paths or historical versions.
        self.api.kernels_output(versioned_reference(job), path=str(output), force=False, quiet=True,
                                file_pattern=r"^(?:model\.keras|preprocessor\.json|samples\.npz|report\.json|manifest\.json)$")
        separate_sdk_log(directory, job)
        return output

    def cancel(self, directory, session_id, confirmed_reference, request_factory=None):
        directory, job = load_job(directory)
        reference = versioned_reference(job)
        if (job["purpose"] != "cancel" or confirmed_reference != reference
                or type(session_id) is not int or session_id < 1):
            raise ProbeError("Cancellation requires the dedicated cancel probe and explicit exact version confirmation.")
        if session_id == job.get("kernel_id"):
            raise ProbeError("The notebook ID cannot be used as a session ID.")
        if job.get("session_identity_verified") is not True or job.get("session_id") != session_id:
            raise ProbeError("No verified job-to-session mapping. Automatic cancellation is not ready; do not guess a session ID.")
        if self.status(directory)["state"] not in {"RUNNING", "QUEUED"}:
            raise ProbeError("The cancellation probe is not running or queued.")
        if request_factory is None:
            from kagglesdk.kernels.types.kernels_api_service import ApiCancelKernelSessionRequest
            request_factory = ApiCancelKernelSessionRequest
        request = request_factory()
        request.kernel_session_id = session_id
        with self.api.build_kaggle_client() as client:
            response = client.kernels.kernels_api_client.cancel_kernel_session(request)
        if getattr(response, "error_message", ""):
            raise ProbeError("Kaggle rejected the cancellation request. Raw provider message omitted.")
        job["cancel_requested"] = True
        job["session_id"] = session_id
        # A mapping must come from verified provider evidence before this call;
        # command-line assertions never establish ownership of a session.
        update_job(directory, {"cancel_requested": True, "session_id": session_id})
        return {"reference": reference, "cancel_requested": True, "cancel_confirmed": False,
                "production_ready": False}

    def confirm_cancel(self, directory):
        directory, job = load_job(directory)
        state = self.status(directory)["state"]
        if job["purpose"] == "cooperative-cancel":
            confirmed = (bool(job.get("cooperative_cancel_requested"))
                         and bool(job.get("cooperative_child_running_observed"))
                         and bool(job.get("cooperative_cancel_ack_observed")) and state == "COMPLETE")
        else:
            confirmed = bool(job.get("cancel_requested")) and state == "CANCEL_ACKNOWLEDGED"
        update_job(directory, {"cancel_confirmed": confirmed})
        return {"state": state, "cancel_confirmed": confirmed,
                "cancellation_method": "cooperative" if job["purpose"] == "cooperative-cancel" else "native_session_api",
                "automatic_session_identity_verified": False, "production_ready": False}

    def cooperative_cancel(self, directory):
        """Publish an exact cancel marker via Quick Save, not a second run.

        This is a CPU-only capability test. It needs no guessed session ID,
        public callback or embedded token. Confirmation is a separate step.
        """
        directory, job = load_job(directory)
        if job["purpose"] != "cooperative-cancel":
            raise ProbeError("This operation is only for the dedicated cooperative cancellation probe.")
        versioned_reference(job)
        request_factory = self.kernel_request_factory
        if request_factory is None:
            from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest
            request_factory = ApiGetKernelRequest
        request = request_factory()
        request.user_name, request.kernel_slug = job["reference"].split("/")

        def remote_source():
            with self.api.build_kaggle_client() as client:
                remote = client.kernels.kernels_api_client.get_kernel(request)
            if (remote.metadata.ref != job["reference"] or remote.metadata.is_private is not True
                    or remote.metadata.enable_gpu is not False
                    or remote.metadata.enable_internet is not True):
                raise ProbeError("Cooperative control notebook identity/privacy/resources differ.")
            return remote

        original = (directory / "probe.py").read_text(encoding="utf-8").replace("\r\n", "\n")
        signalled = original.rstrip() + "\n\n# TRAFFIC_POC_CANCEL " + job["job_id"] + "\n"
        remote = remote_source()
        source = remote.blob.source.replace("\r\n", "\n")
        if source == signalled and remote.metadata.current_version_number > job["version"]:
            update_job(directory, {"cooperative_cancel_requested": True, "cancel_signal_pending": False,
                                   "cancel_signal_version": remote.metadata.current_version_number})
            return {"cancel_signal_published": True, "cancel_confirmed": False, "new_training_run_started": False}
        if source != original or remote.metadata.current_version_number != job["version"]:
            raise ProbeError("The remote control source changed unexpectedly; no cancellation signal was written.")
        try:
            fd = os.open(directory / "cancel-signal.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise ProbeError("Cancellation signal publication is uncertain; reconcile it rather than publishing twice.") from None
        os.close(fd)
        control = directory / "cancel-version"
        control.mkdir(exist_ok=False)
        metadata = json.loads((directory / "kernel-metadata.json").read_text(encoding="utf-8"))
        write_json(control / "kernel-metadata.json", metadata)
        (control / "probe.py").write_text(signalled, encoding="utf-8")
        update_job(directory, {"cancel_signal_pending": True})
        result = self.api.kernels_push(str(control), no_run=True)
        if getattr(result, "error", ""):
            raise ProbeError("The provider did not accept the cooperative cancel signal. Cancellation is not confirmed.")
        remote = remote_source()
        if (remote.blob.source.replace("\r\n", "\n") != signalled
                or remote.metadata.current_version_number <= job["version"]):
            raise ProbeError("The cooperative signal has not been verified on Kaggle.")
        update_job(directory, {"cooperative_cancel_requested": True, "cancel_signal_pending": False,
                               "cancel_signal_version": remote.metadata.current_version_number})
        return {"cancel_signal_published": True, "cancel_confirmed": False, "new_training_run_started": False}
