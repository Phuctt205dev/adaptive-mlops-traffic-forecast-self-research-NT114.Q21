"""Content-addressed branch bundles for the RKE2 deployment.

This is transport only: never import a training backend or deserialize models.
A receipt identifies one exact manifest, with no mutable 'latest' pointer.
Publish the manifest LAST; restore to a new directory only after verification.
The training runtime does not use this module until a later reviewed stage.
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import uuid


CHUNK = 128 * 1024
MAX_MANIFEST = 1024 * 1024
DIGEST = re.compile(r"^[0-9a-f]{64}$")
BRANCHES = {"tree", "lstm", "gru"}


class ArtifactError(Exception):
    """Fixed diagnostic text; do not include SDK errors/credentials."""


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ArtifactError("Artifact metadata is not finite JSON") from error


def identity(region_id, run_id, branch, bundle_id):
    try:
        if any(str(uuid.UUID(value)) != value for value in (region_id, run_id, bundle_id)):
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as error:
        raise ArtifactError("Invalid artifact identity") from error
    if not isinstance(branch, str) or branch not in BRANCHES:
        raise ArtifactError("Invalid artifact branch")
    return {"version": 1, "region_id": region_id, "run_id": run_id, "branch": branch, "bundle_id": bundle_id}


def relative_name(value):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise ArtifactError("Invalid artifact path")
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or str(path) != value or "\\" in value or ":" in value
            or any(ord(character) < 32 for character in value)
            or any(part in (".", "..") or part.startswith(".") for part in path.parts)):
        raise ArtifactError("Invalid artifact path")
    return value


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class BranchArtifacts:
    def __init__(self, client, bucket, *, prefix="training-exchange/v1",
                 max_total_bytes=1536 * 1024 * 1024, max_files=128):
        # Keep artifacts away from the legacy API sidecars' six sync roots.
        if prefix != "training-exchange/v1":
            match = re.fullmatch(r"training-exchange-probes/v1/([0-9a-f-]{36})", prefix)
            if not match:
                raise ArtifactError("Invalid artifact namespace")
            try:
                if str(uuid.UUID(match[1])) != match[1]:
                    raise ValueError()
            except ValueError as error:
                raise ArtifactError("Invalid probe namespace") from error
        if type(max_total_bytes) is not int or max_total_bytes <= 0 or type(max_files) is not int or max_files <= 0:
            raise ArtifactError("Invalid transfer budget")
        self.client, self.bucket, self.prefix = client, bucket, prefix
        self.max_total_bytes, self.max_files = max_total_bytes, max_files

    def base(self, info):
        return (f"{self.prefix}/regions/{info['region_id']}/runs/{info['run_id']}"
                f"/branches/{info['branch']}/bundles/{info['bundle_id']}")

    def object_key(self, info, item):
        return self.base(info) + "/objects/" + item["sha256"] + "/" + item["path"]

    def validate(self, document):
        if not isinstance(document, dict) or set(document) != {"version", "region_id", "run_id", "branch", "bundle_id", "result", "files"}:
            raise ArtifactError("Unexpected manifest structure")
        info = identity(document["region_id"], document["run_id"], document["branch"], document["bundle_id"])
        if type(document["version"]) is not int or document["version"] != 1 or not isinstance(document["result"], dict):
            raise ArtifactError("Unsupported manifest version/result")
        files = document["files"]
        if not isinstance(files, list) or len(files) > self.max_files:
            raise ArtifactError("Artifact file count exceeds budget")
        seen, total = set(), 0
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
                raise ArtifactError("Unexpected file descriptor")
            name = relative_name(item["path"])
            if name in seen or any(name.startswith(old + "/") or old.startswith(name + "/") for old in seen):
                raise ArtifactError("Conflicting artifact paths")
            seen.add(name)
            if type(item["size"]) is not int or item["size"] <= 0:
                raise ArtifactError("Empty/invalid artifact file")
            if not isinstance(item["sha256"], str) or not DIGEST.fullmatch(item["sha256"]):
                raise ArtifactError("Invalid artifact checksum")
            total += item["size"]
        if total > self.max_total_bytes:
            raise ArtifactError("Artifact bytes exceed transfer budget")
        return info

    def publish(self, region_id, run_id, branch, result, files, *, source_root):
        # A fresh bundle per publication also isolates a failed/mutated upload
        # from all previously committed results, including retries of one branch.
        info = identity(region_id, run_id, branch, str(uuid.uuid4()))
        if not isinstance(files, dict) or len(files) > self.max_files:
            raise ArtifactError("Invalid artifact file mapping")
        root = Path(source_root).resolve(strict=True)
        paths, entries, total = {}, [], 0
        for name, filename in sorted(files.items()):
            relative_name(name)
            original = Path(filename)
            # Reject symlinks in the complete caller-supplied path before resolving.
            if any(part.is_symlink() for part in (original, *original.parents)):
                raise ArtifactError("Symlink source is not allowed")
            path = original.resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ArtifactError("Artifact source is outside its allowed root")
            size = path.stat().st_size
            total += size
            if size <= 0 or total > self.max_total_bytes:
                raise ArtifactError("Artifact source exceeds transfer budget or is empty")
            paths[name] = path
            entries.append({"path": name, "size": size, "sha256": file_digest(path)})
        document = {**info, "result": result, "files": entries}
        self.validate(document)
        raw = canonical(document)
        if len(raw) > MAX_MANIFEST:
            raise ArtifactError("Manifest exceeds inspection budget")
        digest = hashlib.sha256(raw).hexdigest()
        for item in entries:
            path = paths[item["path"]]
            with path.open("rb") as source:
                self.client.put_object(Bucket=self.bucket, Key=self.object_key(info, item), Body=source,
                                       ContentLength=item["size"], ContentType="application/octet-stream",
                                       Metadata={"sha256": item["sha256"]})
            # Training outputs must be stable for publication; no partial manifest.
            if path.stat().st_size != item["size"] or file_digest(path) != item["sha256"]:
                raise ArtifactError("Artifact source changed during upload")
            head = self.client.head_object(Bucket=self.bucket, Key=self.object_key(info, item))
            if head.get("ContentLength") != item["size"] or head.get("Metadata", {}).get("sha256") != item["sha256"]:
                raise ArtifactError("Uploaded artifact metadata differs")
        self.client.put_object(Bucket=self.bucket, Key=self.base(info) + "/manifests/" + digest + ".json",
                               Body=raw, ContentLength=len(raw), ContentType="application/json",
                               Metadata={"sha256": digest})
        return {**info, "manifest_sha256": digest}

    def load_manifest(self, receipt):
        if not isinstance(receipt, dict) or set(receipt) != {"version", "region_id", "run_id", "branch", "bundle_id", "manifest_sha256"}:
            raise ArtifactError("Invalid artifact receipt")
        info = identity(receipt["region_id"], receipt["run_id"], receipt["branch"], receipt["bundle_id"])
        digest = receipt["manifest_sha256"]
        if type(receipt["version"]) is not int or receipt["version"] != 1 or not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ArtifactError("Invalid receipt version/checksum")
        response = self.client.get_object(Bucket=self.bucket,
            Key=self.base(info) + "/manifests/" + digest + ".json")
        with closing(response["Body"]) as body:
            raw = body.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST or hashlib.sha256(raw).hexdigest() != digest:
            raise ArtifactError("Manifest content checksum differs")
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise ArtifactError("Invalid manifest JSON") from error
        if self.validate(document) != info:
            raise ArtifactError("Manifest identity differs from receipt")
        # Re-encoding rejects nonfinite numbers, even if a remote manifest was forged.
        canonical(document)
        return document

    def restore(self, receipt, destination):
        destination = Path(destination).absolute()
        if destination.exists() or destination.is_symlink():
            raise ArtifactError("Restore requires a new destination directory")
        # Never resolve or write through a pre-existing symlink parent.
        if any(part.is_symlink() for part in destination.parents) or not destination.parent.is_dir():
            raise ArtifactError("Invalid restore parent directory")
        document = self.load_manifest(receipt)
        temporary = Path(tempfile.mkdtemp(prefix=".artifact-receive-", dir=destination.parent))
        try:
            for item in document["files"]:
                path = temporary / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                response = self.client.get_object(Bucket=self.bucket, Key=self.object_key(document, item))
                digest, size = hashlib.sha256(), 0
                with closing(response["Body"]) as source, path.open("xb") as output:
                    while chunk := source.read(CHUNK):
                        size += len(chunk)
                        if size > item["size"]:
                            raise ArtifactError("Downloaded artifact exceeds declared size")
                        digest.update(chunk)
                        output.write(chunk)
                if size != item["size"] or digest.hexdigest() != item["sha256"]:
                    raise ArtifactError("Downloaded artifact content differs")
            # No consumer sees the bundle until every file has passed validation.
            if destination.exists() or destination.is_symlink():
                raise ArtifactError("Restore destination appeared during transfer")
            os.rename(temporary, destination)
            return document
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
