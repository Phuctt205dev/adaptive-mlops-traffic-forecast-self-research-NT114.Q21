"""Build a private, explicit ML source/data snapshot; never archive the repo."""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import uuid
import zipfile

from src.cloud_training.artifacts import relative_path
from src.cloud_training.probe import canonical, sha256_file

MAX_DATASET_BYTES = 100 * 1024 * 1024
MAX_CODE_BYTES = 4 * 1024 * 1024
CONTEXT_KEYS = {"run_id", "region_id", "dataset_id", "attempt_id", "branch", "configuration_sha256"}


def configuration_digest(configuration):
    # Must match the existing RKE2 receipt bridge, including Unicode handling.
    return hashlib.sha256(json.dumps(configuration, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def validate_context(context):
    if not isinstance(context, dict) or set(context) != CONTEXT_KEYS:
        raise ValueError("Invalid execution context fields.")
    for key in ("run_id", "region_id", "dataset_id", "attempt_id"):
        if str(uuid.UUID(context[key])) != context[key]:
            raise ValueError("Execution context requires canonical UUIDs.")
    digest = context["configuration_sha256"]
    if (context["branch"] not in {"tree", "lstm", "gru"} or not isinstance(digest, str)
            or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
        raise ValueError("Invalid execution branch/configuration hash.")


def ml_sources(repository):
    """Follow static src imports from the pure runner, including package init.

    The ML package has no backend imports. Refuse such a dependency instead of
    accidentally including credentials, API/database code or deployment files.
    """
    root = Path(repository).resolve(strict=True)
    pending = ["scripts/cloud_training/execute_branch.py"]
    visited = {}

    def enqueue_module(module):
        if module == "src" or module.startswith("src."):
            parts = module.split(".")
            for length in range(1, len(parts) + 1):
                base = Path(*parts[:length])
                for candidate in (base.with_suffix(".py"), base / "__init__.py"):
                    if (root / candidate).is_file():
                        pending.append(candidate.as_posix())

    while pending:
        name = relative_path(pending.pop())
        if name in visited:
            continue
        path = root / name
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("Source snapshots must not follow symlinks.")
        source = path.read_bytes()
        visited[name] = source
        for node in ast.walk(ast.parse(source, filename=name)):
            modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else [item.name for item in node.names] if isinstance(node, ast.Import) else []
            for module in modules:
                if module == "backend" or module.startswith("backend."):
                    raise ValueError("The pure ML bundle cannot depend on backend services.")
                enqueue_module(module)
                if isinstance(node, ast.ImportFrom):
                    for item in node.names:
                        enqueue_module(module + "." + item.name)
    if sum(map(len, visited.values())) > MAX_CODE_BYTES or len(visited) > 100:
        raise ValueError("ML source snapshot exceeds its size budget.")
    return visited


def prepare_package(repository, dataset, configuration, context, destination, dataset_sha256, runtime_profile=None):
    validate_context(context)
    if configuration_digest(configuration) != context["configuration_sha256"]:
        raise ValueError("Execution configuration differs from its identity.")
    dataset = Path(dataset)
    if (not dataset.is_file() or any(part.is_symlink() for part in (dataset, *dataset.parents))
            or not 0 < dataset.stat().st_size <= MAX_DATASET_BYTES or sha256_file(dataset) != dataset_sha256):
        raise ValueError("Dataset snapshot size/hash/path differs from the registered dataset.")
    sources = ml_sources(repository)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    # Kaggle expands .zip dataset attachments automatically. A binary extension
    # keeps this hash-bound archive intact for the supervisor to verify/extract.
    with zipfile.ZipFile(destination / "source.bin", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, source in sorted(sources.items()):
            archive.writestr(name, source)
    shutil.copyfile(dataset, destination / "dataset.csv")
    if sha256_file(destination / "dataset.csv") != dataset_sha256:
        raise ValueError("Dataset changed while preparing its snapshot.")
    job = {"context": context, "configuration": configuration, "dataset_sha256": dataset_sha256}
    if runtime_profile is not None:
        job["runtime_profile"] = runtime_profile
    (destination / "job.json").write_bytes(canonical(job))
    manifest = {name: {"sha256": sha256_file(destination / name), "size": (destination / name).stat().st_size}
                for name in ("source.bin", "dataset.csv", "job.json")}
    return {"context": context, "files": manifest}
