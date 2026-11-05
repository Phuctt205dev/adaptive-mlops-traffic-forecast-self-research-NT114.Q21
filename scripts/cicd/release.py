"""Build and verify an explicit, credential-free release from tracked sources.

The image extends the deployed dependency image. Its payload is installed into
fresh pod volumes by an init container; older source mounts cannot hide it.
No cluster calls, training, database changes or image publication happen here.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.cloud_training.prepare_rollout import SDK_LOCK, canonical, code_map

SCHEMA = "traffic-release/v1"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST_IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}")
RUNTIME_FILES = ("cloud_training_runtime.py", "traffic_region_training_cloud.py", "traffic_region_training_worker.py")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def safe_path(name):
    path = PurePosixPath(name)
    if not isinstance(name, str) or not name or path.is_absolute() or "\\" in name or ":" in name or any(p in {".", ".."} for p in name.split("/")):
        raise ValueError("Release path is unsafe.")
    return path


def tracked_sources(root):
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
    return set(result.stdout.decode("utf-8").split("\0")) - {""}


def source_files(root, *, tracked=None):
    root = Path(root).resolve()
    tracked = tracked_sources(root) if tracked is None else set(tracked)
    names = sorted(name for name in tracked if name.startswith(("backend/", "src/")) and name.endswith(".py"))
    names += ["app.py", "scripts/cloud_training/execute_branch.py", "scripts/cloud_training/verify_execution.py"]
    names += ["scripts/cicd/maintenance.py", "docs/index.html"]
    names += ["deployment/rke2/runtime/" + name for name in RUNTIME_FILES]
    files = {}
    for name in names:
        if name not in tracked:
            raise ValueError("Release source must be tracked: " + name)
        relative = safe_path(name)
        path = root.joinpath(*relative.parts)
        if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("Release source is missing or follows a symlink.")
        data = path.read_bytes().replace(b"\r\n", b"\n")
        if len(data) > 512 * 1024:
            raise ValueError("Release source exceeds the file budget.")
        files[name] = data
    if not any(n.startswith("backend/") for n in files) or not any(n.startswith("src/") for n in files):
        raise ValueError("Release source tree is incomplete.")
    return files


def build(root, output, commit, *, tracked=None):
    if not SHA.fullmatch(commit):
        raise ValueError("A full commit SHA is required.")
    root, output = Path(root), Path(output)
    if output.exists():
        raise ValueError("Use a fresh release directory.")
    sources = source_files(root, tracked=tracked)
    baseline = read_json(root / "deployment/rke2/baseline.json")
    if not DIGEST_IMAGE.fullmatch(baseline["application_base_image"]):
        raise ValueError("The dependency image must be pinned by digest.")
    for name, expected in baseline.get("dependency_contract", {}).items():
        if digest((root / safe_path(name)).read_bytes().replace(b"\r\n", b"\n")) != expected:
            raise ValueError("Dependency/schema contract changed; prepare a reviewed base-image or migration upgrade: " + name)
    cloud = {"project/" + name: data for name, data in sources.items()
             if name.startswith(("backend/", "src/", "scripts/")) or name == "app.py"}
    # Keep namespace/package initializers in the payload, but import packages
    # from the proven image. Overlay leaf modules before their first import.
    cloud["cloud_training_runtime.py"] = sources["deployment/rke2/runtime/cloud_training_runtime.py"]
    cloud["project/docs/index.html"] = sources["docs/index.html"]
    cloud["sdk-requirements.txt"] = SDK_LOCK.encode()
    _, _, manifest = code_map(cloud)
    manifest["modules"] = {name: path for name, path in manifest["modules"].items()
                           if not path.endswith("/__init__.py")}
    manifest["modules"]["app"] = "project/app.py"
    manifest.pop("bundle_id")
    manifest["bundle_id"] = digest(canonical(manifest))
    cloud["manifest.json"] = canonical(manifest)
    airflow = {"dag.py": sources["deployment/rke2/runtime/traffic_region_training_cloud.py"],
               "legacy/traffic_region_training_worker.py": sources["deployment/rke2/runtime/traffic_region_training_worker.py"],
               "airflow_control.py": sources["src/cloud_training/airflow_control.py"]}
    payload = {**{"payload/cloud/" + n: v for n, v in cloud.items()},
               **{"payload/airflow/" + n: v for n, v in airflow.items()}}
    payload["payload/maintenance.py"] = sources["scripts/cicd/maintenance.py"]
    # Only these files enter the image context. Never COPY the developer repo.
    payload["Dockerfile"] = ("FROM " + baseline["application_base_image"] + "\n"
        "COPY payload/ /opt/traffic-release/\n"
        "COPY release.json /opt/traffic-release/release.json\n"
        "LABEL org.opencontainers.image.revision=" + commit + "\n").encode()
    payload[".dockerignore"] = b"**\n!Dockerfile\n!release.json\n!payload/\n!payload/**\n"
    document = {"schema": SCHEMA, "commit": commit, "baseline": baseline,
                "cloud_bundle_id": manifest["bundle_id"], "airflow_sha256": digest(canonical({n: digest(v) for n, v in airflow.items()})),
                "files": {n: digest(v) for n, v in sorted(payload.items())},
                "source_hashes": {n: digest(v) for n, v in sorted(sources.items())}}
    document["release_id"] = digest(canonical(document))
    for name, data in payload.items():
        path = output.joinpath(*safe_path(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (output / "release.json").write_bytes(canonical(document))
    verify(output, expected_commit=commit)
    return document


def verify(directory, *, expected_commit=None):
    root = Path(directory).resolve(strict=True)
    document = read_json(root / "release.json")
    if document.get("schema") != SCHEMA or not SHA.fullmatch(document.get("commit", "")):
        raise ValueError("Unsupported release metadata.")
    if expected_commit is not None and document["commit"] != expected_commit:
        raise ValueError("Release belongs to a different commit.")
    identity = {k: v for k, v in document.items() if k != "release_id"}
    if document.get("release_id") != digest(canonical(identity)):
        raise ValueError("Release metadata was modified.")
    expected = set(document["files"]) | {"release.json"}
    if any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError("Release directories cannot contain symlinks.")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != expected:
        raise ValueError("Release has missing or unexpected files.")
    for name, value in document["files"].items():
        path = root.joinpath(*safe_path(name).parts)
        if any(p.is_symlink() for p in (path, *path.parents)) or digest(path.read_bytes()) != value:
            raise ValueError("Release file hash/path differs.")
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("Build", "Verify"))
    parser.add_argument("--directory", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    document = build(ROOT, args.directory, args.commit) if args.action == "Build" else verify(args.directory, expected_commit=args.commit)
    print(json.dumps({k: document[k] for k in ("commit", "release_id", "cloud_bundle_id")}))


if __name__ == "__main__":
    main()
