"""Portable output contract; files are validated before any deserialization."""
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath

from src.cloud_training.probe import canonical, sha256_file

FILE_FIELDS = {"model_file", "versioned_model_file", "preprocessor_file", "source_report", "compatibility_file"}
MAX_FILES = 128
MAX_BYTES = 1024 * 1024 * 1024
MARKER = "$training_file"


class ArtifactError(ValueError):
    pass


def relative_path(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value
            or PurePosixPath(value).is_absolute() or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ArtifactError("Unsafe artifact relative path.")
    return value


def transform_files(value, convert):
    if isinstance(value, list):
        return [transform_files(item, convert) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    if MARKER in value:
        raise ArtifactError("Reserved artifact marker in producer output.")
    result = {}
    for key, item in value.items():
        if key in FILE_FIELDS and item is not None:
            result[key] = convert(item)
        elif key == "artifact_paths":
            if not isinstance(item, list):
                raise ArtifactError("Invalid artifact path list.")
            result[key] = [convert(path) for path in item]
        elif key in {"mlflow_run_id", "mlflow_model_uri"}:
            # An offline runs:/ URI must not masquerade as a central MLflow run.
            result[key] = None
        else:
            result[key] = transform_files(item, convert)
    return result


def write_bundle(result, context, root):
    root = Path(root).resolve(strict=True)
    files = {}

    def convert(value):
        if not isinstance(value, str):
            raise ArtifactError("Invalid artifact source.")
        original = Path(value)
        if any(part.is_symlink() for part in (original, *original.parents)):
            raise ArtifactError("Artifact symlink is not allowed.")
        source = original.resolve(strict=True)
        if not source.is_file() or not source.is_relative_to(root):
            raise ArtifactError("Artifact is outside the execution staging root.")
        name = relative_path(source.relative_to(root).as_posix())
        files[name] = {"path": name, "size": source.stat().st_size, "sha256": sha256_file(source)}
        return {MARKER: name}

    encoded = transform_files(result, convert)
    if len(files) > MAX_FILES or sum(item["size"] for item in files.values()) > MAX_BYTES:
        raise ArtifactError("Execution output exceeds the artifact budget.")
    manifest = {"schema": 1, "context": context, "result": encoded, "files": list(files.values())}
    temporary = root / "manifest.json.tmp"
    temporary.write_bytes(canonical(manifest))
    temporary.replace(root / "manifest.json")
    return manifest


def read_bundle(root, context):
    root = Path(root).resolve(strict=True)
    path = root / "manifest.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ArtifactError("Execution manifest is missing or too large.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    canonical(manifest)  # Reject NaN and infinity.
    if set(manifest) != {"schema", "context", "result", "files"} or type(manifest["schema"]) is not int or manifest["schema"] != 1 or manifest["context"] != context:
        raise ArtifactError("Execution manifest identity differs from the run/attempt/configuration.")
    entries = manifest["files"]
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise ArtifactError("Invalid execution artifact file list.")
    total, allowed = 0, set()
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ArtifactError("Invalid execution file descriptor.")
        name = relative_path(item["path"])
        if name in allowed or type(item["size"]) is not int or item["size"] < 1:
            raise ArtifactError("Duplicate or invalid execution file.")
        total += item["size"]
        if total > MAX_BYTES:
            raise ArtifactError("Execution artifact budget exceeded.")
        allowed.add(name)
        source = root / name
        if any(part.is_symlink() for part in (source, *source.parents)) or not source.is_file() or source.stat().st_size != item["size"] or sha256_file(source) != item["sha256"]:
            raise ArtifactError("Execution artifact is missing, outside staging, or corrupted.")
    used = set()

    def decode(value):
        if isinstance(value, list):
            return [decode(item) for item in value]
        if not isinstance(value, dict):
            return value
        if MARKER in value:
            if set(value) != {MARKER} or not isinstance(value[MARKER], str) or value[MARKER] not in allowed:
                raise ArtifactError("Result references an unverified artifact.")
            used.add(value[MARKER])
            return str(root / value[MARKER])
        for key, item in value.items():
            if key in FILE_FIELDS and item is not None and not (isinstance(item, dict) and set(item) == {MARKER}):
                raise ArtifactError("Result contains an unbound file path.")
            if key == "artifact_paths" and (not isinstance(item, list) or any(
                    not isinstance(entry, dict) or set(entry) != {MARKER} for entry in item)):
                raise ArtifactError("Artifact list contains an unbound file path.")
        return {key: decode(item) for key, item in value.items()}

    result = decode(manifest["result"])
    if used != allowed:
        raise ArtifactError("Manifest contains unreferenced artifacts.")
    return result
