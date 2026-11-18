"""CPU Job bootstrap: verify immutable input, run one model, upload manifest last."""
import json
import os
from pathlib import Path
import sys
import time
import zipfile

import boto3
from botocore.config import Config

from src.cloud_training.artifacts import read_bundle, relative_path
from src.cloud_training.package import MAX_CODE_BYTES, MAX_DATASET_BYTES, validate_context
from src.cloud_training.probe import canonical, sha256_file
from src.cloud_training.process import ExecutionProcess, PREFIX
from src.local_training.kubernetes import MODELS, identity


def get_file(client, bucket, key, path, limit):
    response = client.get_object(Bucket=bucket, Key=key)
    if response.get("ContentLength", limit + 1) > limit:
        response["Body"].close()
        raise ValueError("Input exceeds its byte budget.")
    with response["Body"] as source, path.open("xb") as output:
        size = 0
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError("Input exceeds its byte budget.")
            output.write(chunk)


def main():
    context = json.loads(os.environ["LOCAL_CONTEXT"])
    validate_context(context)
    model, prefix, bucket = (os.environ[k] for k in ("LOCAL_MODEL", "LOCAL_INPUT_PREFIX", "LOCAL_BUCKET"))
    expected = f"local-training/v1/{context['run_id']}/{context['branch']}/{context['attempt_id']}/"
    if prefix != expected or model not in MODELS:
        raise ValueError("Local Job identity differs.")
    work = Path("/work")
    inputs, sources, output = work / "input", work / "source", work / "output"
    inputs.mkdir(exist_ok=False)
    sources.mkdir(exist_ok=False)
    client = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"], config=Config(
        connect_timeout=5, read_timeout=20, retries={"max_attempts": 2, "mode": "standard"}))
    snapshot_path = work / "snapshot.json"
    get_file(client, bucket, prefix + "snapshot.json", snapshot_path, 1024 * 1024)
    snapshot = json.loads(snapshot_path.read_text())
    if snapshot.get("context") != context or set(snapshot.get("files", {})) != {"job.json", "dataset.csv", "source.bin"}:
        raise ValueError("Local input manifest differs.")
    for name, maximum in (("job.json", 1024 * 1024), ("dataset.csv", MAX_DATASET_BYTES), ("source.bin", MAX_CODE_BYTES)):
        path = inputs / name
        get_file(client, bucket, prefix + "input/" + name, path, maximum)
        descriptor = snapshot["files"][name]
        if sha256_file(path) != descriptor["sha256"] or path.stat().st_size != descriptor["size"]:
            raise ValueError("Local input hash differs.")
    job = json.loads((inputs / "job.json").read_text())
    if job.get("context") != context or job["configuration"].get("training_backend") != "local":
        raise ValueError("A Local Job requires a matching local configuration.")
    job["local_model"] = model
    (inputs / "job.json").write_bytes(canonical(job))
    with zipfile.ZipFile(inputs / "source.bin") as archive:
        entries = archive.infolist()
        if len(entries) > 100 or sum(e.file_size for e in entries) > MAX_CODE_BYTES:
            raise ValueError("Local source archive exceeds its budget.")
        for entry in entries:
            name = relative_path(entry.filename)
            if not name.endswith(".py") or not name.startswith(("src/", "scripts/cloud_training/")):
                raise ValueError("Unexpected source archive member.")
            destination = sources / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(entry))
    child = ExecutionProcess([sys.executable, "-u", "-B", str(sources / "scripts/cloud_training/execute_branch.py"),
        "--job", str(inputs / "job.json"), "--dataset", str(inputs / "dataset.csv"), "--output", str(output)], context, cwd=sources)
    try:
        while child.returncode() is None:
            for event in child.drain():
                print(PREFIX + json.dumps({"context": context, "event": event}, allow_nan=False), flush=True)
            time.sleep(.2)
        for event in child.drain():
            print(PREFIX + json.dumps({"context": context, "event": event}, allow_nan=False), flush=True)
        if child.returncode() != 0 or child.reader_failed:
            raise RuntimeError("The model training process did not finish successfully.")
    finally:
        child.stop()
    read_bundle(output, context)
    manifest = json.loads((output / "manifest.json").read_text())
    name = "local-train-" + identity(context, model)[:32]
    destination = prefix + "output/" + name + "/"
    for entry in manifest["files"]:
        client.upload_file(str(output / entry["path"]), bucket, destination + entry["path"])
    client.upload_file(str(output / "manifest.json"), bucket, destination + "manifest.json")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"local_training_failed": True, "error_type": type(error).__name__}), flush=True)
        raise SystemExit(2)
