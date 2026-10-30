"""Small SDK subprocess: bounded by the caller, with sanitized stdout only."""
import contextlib
import json
import os
from pathlib import Path
import sys
import time


class Discard:
    def write(self, value):
        return len(value)

    def flush(self):
        pass


def install_http_limits():
    """Bound authentication too; retry only an undelivered TCP connection.

    SDK requests explicitly pass timeout=None. setdefault therefore did not
    impose a timeout. Read/response failures remain ambiguous for writes and
    must be reconciled by the durable provider ledger, never sent again here.
    """
    import requests
    send = requests.Session.send
    if getattr(send, "_traffic_bounded", False) is True:
        return

    def bounded_send(session, request, **kwargs):
        timeout = kwargs.get("timeout")
        if timeout is None:
            timeout = (5, 30)
        elif isinstance(timeout, (tuple, list)):
            timeout = (min(timeout[0] or 5, 5), min(timeout[1] or 30, 30))
        else:
            timeout = (min(timeout, 5), min(timeout, 30))
        kwargs["timeout"] = timeout
        for attempt in range(3):
            try:
                return send(session, request, **kwargs)
            except requests.exceptions.ConnectTimeout:
                if attempt == 2:
                    raise
                time.sleep(0.25 * (attempt + 1))

    bounded_send._traffic_bounded = True
    requests.Session.send = bounded_send


def api_for(username):
    install_http_limits()
    token_file = os.environ.get("KAGGLE_TOKEN_FILE")
    if token_file:
        token = Path(token_file).read_text(encoding="utf-8").strip()
        if not token or any(character.isspace() for character in token):
            raise ValueError("Mounted Kaggle credential is invalid.")
        os.environ["KAGGLE_API_TOKEN"] = token
    # The SDK normally prints login diagnostics. Never relay them to Airflow.
    # Importing kaggle already creates and authenticates its API singleton.
    # Reuse it instead of performing a second token introspection per RPC.
    import kaggle
    api = kaggle.api
    if api.config_values.get(api.CONFIG_NAME_USER) != username:
        raise ValueError("Unexpected Kaggle identity.")
    return api


def kernel(api, reference):
    from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest
    owner, slug, *version = reference.split("/")
    request = ApiGetKernelRequest()
    request.user_name, request.kernel_slug = owner, slug
    if version:
        request.version_label = api._kernel_version_label(version[0])
    with api.build_kaggle_client() as client:
        response = client.kernels.kernels_api_client.get_kernel(request)
    metadata = response.metadata
    return {"source": response.blob.source, **{name: getattr(metadata, name) for name in (
        "ref", "current_version_number", "is_private", "enable_gpu", "enable_internet",
        "dataset_data_sources", "kernel_data_sources", "competition_data_sources", "model_data_sources")}}


def operation(api, name, request, stdout):
    if name == "doctor":
        quota = api.quota_view()
        gpu = quota.gpu_quota
        return {"authenticated": True, "username": request["username"],
                "gpu_hours_remaining": max(0.0, (gpu.total_time_allowed - gpu.time_used).total_seconds() / 3600) if gpu else None}
    if name == "owned_notebooks":
        refs = []
        for page in range(1, 21):
            items = api.kernels_list(mine=True, page=page, page_size=100)
            refs.extend(item.ref for item in items)
            if len(items) < 100:
                return {"references": refs, "complete": True}
        raise ValueError("Owned notebook listing exceeded its budget.")
    if name == "owned_datasets":
        refs = []
        for page in range(1, 101):
            items = api.dataset_list(mine=True, page=page)
            refs.extend(item.ref for item in items)
            if len(items) < 20:
                return {"references": refs, "complete": True}
        raise ValueError("Owned dataset listing exceeded its budget.")
    if name == "resource_exists":
        try:
            if request["kind"] == "notebook":
                kernel(api, request["reference"])
            elif request["kind"] == "dataset":
                from kagglesdk.datasets.types.dataset_api_service import ApiGetDatasetMetadataRequest
                query = ApiGetDatasetMetadataRequest()
                query.owner_slug, query.dataset_slug = request["reference"].split("/")
                with api.build_kaggle_client() as client:
                    result = client.datasets.dataset_api_client.get_dataset_metadata(query)
                if result.error_message:
                    raise ValueError("Dataset presence could not be confirmed.")
            else:
                raise ValueError("Unknown resource kind.")
            return {"exists": True}
        except Exception as error:
            if getattr(getattr(error, "response", None), "status_code", None) == 404:
                return {"exists": False}
            if getattr(getattr(error, "response", None), "status_code", None) == 403:
                owned = operation(api, "owned_datasets" if request["kind"] == "dataset" else "owned_notebooks", {}, stdout)
                return {"exists": request["reference"] in owned["references"]}
            raise
    if name == "dataset_create":
        response = api.dataset_create_new(request["directory"], public=False, quiet=True, convert_to_csv=False)
        if getattr(response, "error", ""):
            raise ValueError("Dataset submission was not confirmed.")
        return {"created": True}
    if name == "dataset_check":
        from kagglesdk.datasets.types.dataset_api_service import ApiGetDatasetMetadataRequest
        query = ApiGetDatasetMetadataRequest()
        query.owner_slug, query.dataset_slug = request["reference"].split("/")
        with api.build_kaggle_client() as client:
            response = client.datasets.dataset_api_client.get_dataset_metadata(query)
        info = response.info
        if response.error_message or info is None or not info.is_private:
            raise ValueError("Dataset privacy was not confirmed.")
        if (info.owner_user.lower(), info.dataset_slug) != (query.owner_slug.lower(), query.dataset_slug):
            raise ValueError("Dataset identity differs.")
        status = json.loads(api.dataset_status(request["reference"], format="json"))
        return {"private": True, "status": status["status"], "version": status["current_version_number"]}
    if name == "kernel_get":
        return kernel(api, request["reference"])
    if name == "kernel_push":
        response = api.kernels_push(request["directory"], no_run=request["no_run"])
        if getattr(response, "error", "") or any(getattr(response, key, None) for key in (
                "invalid_dataset_sources", "invalid_kernel_sources", "invalid_competition_sources", "invalid_model_sources")):
            version = getattr(response, "version_number", None)
            return {"rejected": True, "version": version if type(version) is int and version > 0 else None}
        version = response.version_number
        if type(version) is not int or version < 1:
            raise ValueError("Notebook version was not confirmed.")
        return {"version": version}
    if name == "status":
        from src.cloud_training.probe import safe_status
        return {"state": safe_status(api.kernels_status(request["reference"]).status)}
    if name == "events":
        from src.cloud_training.errors import validate_failure
        context = request["context"]
        for item in api.kernels_logs_stream(request["reference"]):
            raw = item.get("data", "") if isinstance(item, dict) else ""
            for line in raw.splitlines():
                for prefix in ("TRAFFIC_EXECUTION_EVENT ", "TRAFFIC_CONTROL_EVENT "):
                    if not line.startswith(prefix):
                        continue
                    if len(line) > 16384:
                        raise ValueError("Oversized progress event.")
                    event = json.loads(line[len(prefix):])
                    if event.get("context") != context:
                        raise ValueError("Progress belongs to another execution.")
                    payload = event["event"] if prefix == "TRAFFIC_EXECUTION_EVENT " else {"control": event["stage"]}
                    if prefix == "TRAFFIC_CONTROL_EVENT " and "failure" in event:
                        if event["stage"] != "failed":
                            raise ValueError("Unexpected failure stage.")
                        payload["failure"] = validate_failure(event["failure"])
                    if prefix == "TRAFFIC_CONTROL_EVENT " and "supervisor_failure" in event:
                        status = event.get("supervisor_http_status")
                        phase = event.get("supervisor_phase")
                        child_code = event.get("supervisor_child_returncode")
                        if (event["stage"] != "failed" or phase not in {"startup", "control_check", "input_validation", "child_monitoring"}
                                or status is not None and (type(status) is not int or not 100 <= status <= 599)):
                            raise ValueError("Invalid normalized supervisor failure.")
                        if child_code is not None and (type(child_code) is not int or not -999 <= child_code <= 999):
                            raise ValueError("Invalid normalized child exit code.")
                        payload.update(supervisor_failure=validate_failure(event["supervisor_failure"]),
                                       supervisor_http_status=status, supervisor_phase=phase,
                                       supervisor_child_returncode=child_code)
                    stdout.write("TRAFFIC_EXECUTION_EVENT " + json.dumps({"context": context, "event": payload}, allow_nan=False) + "\n")
                    stdout.flush()
        return {"stream_ended": True}
    if name == "output_list":
        from kagglesdk.kernels.types.kernels_api_service import ApiListKernelSessionOutputRequest
        owner, slug, version = request["reference"].split("/")
        query = ApiListKernelSessionOutputRequest()
        query.user_name, query.kernel_slug = owner, slug
        query.version_label, query.page_size = api._kernel_version_label(version), 100
        files, seen = {}, set()
        with api.build_kaggle_client() as client:
            while True:
                result = client.kernels.kernels_api_client.list_kernel_session_output(query)
                for item in result.files or []:
                    if item.file_name in files or len(files) >= 2048:
                        raise ValueError("Invalid output listing.")
                    files[item.file_name] = item.url
                if not result.next_page_token:
                    break
                if result.next_page_token in seen:
                    raise ValueError("Output listing did not advance.")
                seen.add(result.next_page_token)
                query.page_token = result.next_page_token
        return {"files": files}
    if name == "cleanup":
        # Deterministic per-attempt references checked again by the controller.
        if request.get("notebook"):
            api.kernels_delete(request["notebook"], no_confirm=True)
        if request.get("dataset"):
            owner, slug = request["dataset"].split("/")
            api.dataset_delete(owner, slug, no_confirm=True)
        return {"cleaned": True}
    raise ValueError("Unknown SDK operation.")


def main():
    stdout = sys.stdout
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("SDK request is too large.")
        request = json.loads(raw)
        with contextlib.redirect_stdout(Discard()), contextlib.redirect_stderr(Discard()):
            api = api_for(request["username"])
            result = operation(api, request["operation"], request, stdout)
        stdout.write(json.dumps({"ok": True, "result": result}, allow_nan=False) + "\n")
        return 0
    except (Exception, SystemExit) as error:
        stdout.write(json.dumps({"ok": False, "error": "kaggle_operation_unconfirmed",
            "error_type": type(error).__name__,
            "http_status": getattr(getattr(error, "response", None), "status_code", None)}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
