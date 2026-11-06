"""RKE2 worker hooks: exchange branch results and persist registration files.

Install only in a dedicated worker after verifying the image source hashes.
No training code or backend is imported by this module. Enabling execution,
HTTP receipt handling and API neural-file restore belong to the entrypoints.
"""

from contextlib import closing, contextmanager
from contextvars import ContextVar
import copy
import hashlib
from pathlib import Path
import shutil
import tempfile
import uuid

from backend.app.services.training_artifacts import ArtifactError, BRANCHES, CHUNK, canonical, file_digest, relative_name


FILE_FIELDS = {"model_file", "versioned_model_file", "preprocessor_file", "source_report", "compatibility_file"}
MARKER = "__rke2_training_file_v1__"


def run_context(run):
    result = {}
    for name, value in (("run_id", run.id), ("region_id", run.region_id), ("dataset_id", run.dataset_id)):
        try:
            result[name] = str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as error:
            raise ArtifactError("Invalid training context identity") from error
    result["configuration_sha256"] = hashlib.sha256(canonical(run.configuration_json or {})).hexdigest()
    return result


def transform_paths(value, convert):
    """Transform only documented file fields; leave metrics and dataset URIs alone."""
    if isinstance(value, list):
        return [transform_paths(item, convert) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    if MARKER in value:
        raise ArtifactError("Reserved file marker in training result")
    result = {}
    for key, item in value.items():
        if key in FILE_FIELDS and item is not None:
            result[key] = convert(item)
        elif key == "artifact_paths":
            if not isinstance(item, list) or not all(isinstance(path, str) for path in item):
                raise ArtifactError("Invalid training artifact list")
            result[key] = [convert(path) for path in item]
        else:
            result[key] = transform_paths(item, convert)
    return result


def check_result(run, branch, result):
    if branch not in BRANCHES or not isinstance(result, dict) or result.get("branch") != branch:
        raise ArtifactError("Training result branch differs")
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        raise ArtifactError("Training candidates must be a list")
    config = run.configuration_json or {}
    if config.get('_execution_protocol') == 2 and branch == 'tree' and result.get('status') == 'skipped':
        selected = config.get('selected_models') or []
        if any(name.endswith('_lag') for name in selected) or result.get('candidates') != [] or result.get('production_candidate'):
            raise ArtifactError('Required tree branch cannot be skipped')
        return
    selected = (run.configuration_json or {}).get("selected_models")
    selected = {str(value).strip().lower() for value in selected} if selected else {"lstm", "gru"}
    if result.get("status") == "skipped":
        if branch == "tree" or branch in selected or candidates or result.get("production_candidate"):
            raise ArtifactError("Required training branch cannot be skipped")
        return
    if result.get("status") != "completed" or not candidates:
        raise ArtifactError("Training branch is not complete")
    if branch in {"lstm", "gru"} and branch not in selected:
        raise ArtifactError("Unexpected recurrent training branch")
    for candidate in candidates + ([result["production_candidate"]] if result.get("production_candidate") else []):
        if not isinstance(candidate, dict) or not (candidate.get("versioned_model_file") or candidate.get("model_file")):
            raise ArtifactError("Candidate model file is missing")
        if branch in {"lstm", "gru"}:
            if (candidate.get("model_family") != "neural_sequence" or not candidate.get("preprocessor_file")
                    or str(candidate.get("best_model_name", "")).lower() != branch):
                raise ArtifactError("Neural candidate/preprocessor differs")


class TrainingResultBridge:
    def __init__(self, store, *, artifact_root, receive_root, model_root, serving_root,
                 max_receive_bytes=1024 * 1024 * 1024):
        self.store = store
        self.artifact_root = Path(artifact_root).resolve(strict=True)
        self.receive_root = Path(receive_root).resolve(strict=True)
        self.model_root = Path(model_root).resolve(strict=True)
        self.serving_root = Path(serving_root).resolve(strict=True)
        if not self.serving_root.is_relative_to(self.model_root) or self.serving_root == self.model_root:
            raise ArtifactError("Serving root must be a separate model subdirectory")
        if type(max_receive_bytes) is not int or max_receive_bytes <= 0:
            raise ArtifactError("Invalid combined receive budget")
        self.max_receive_bytes = max_receive_bytes
        self.scope = ContextVar("rke2_training_receipts", default=None)
        self.registration = ContextVar("rke2_training_registration", default=None)
        self.published = ContextVar("rke2_training_published_receipt", default=None)

    def encode(self, result):
        files = {}

        def convert(filename):
            if not isinstance(filename, str) or not filename:
                raise ArtifactError("Invalid candidate file reference")
            original = Path(filename)
            if any(path.is_symlink() for path in (original, *original.parents)):
                raise ArtifactError("Symlink candidate reference is not allowed")
            path = original.resolve(strict=True)
            if not path.is_relative_to(self.artifact_root) or not path.is_file():
                raise ArtifactError("Candidate file is outside the training artifact root")
            name = relative_name(path.relative_to(self.artifact_root).as_posix())
            files[name] = path
            return {MARKER: name}

        encoded = transform_paths(result, convert)
        # Preserve a local MLflow URI only when it references an exchanged file.
        mapping = {str(path): {MARKER: name} for name, path in files.items()}

        def mlflow_paths(value):
            if isinstance(value, list):
                return [mlflow_paths(item) for item in value]
            if not isinstance(value, dict):
                return value
            return {key: mapping.get(str(Path(item).resolve()), item)
                    if key == "mlflow_model_uri" and isinstance(item, str) and not item.startswith(("runs:", "models:", "s3:"))
                    else mlflow_paths(item) for key, item in value.items()}

        return mlflow_paths(encoded), files

    def publish_branch(self, run, branch, result):
        check_result(run, branch, result)
        context = run_context(run)
        encoded, files = self.encode(result)
        envelope = {"schema": 1, "context": context, "branch_result": encoded}
        return self.store.publish(context["region_id"], context["run_id"], branch, envelope, files,
                                  source_root=self.artifact_root)

    def decode(self, encoded, document, directory):
        allowed = {item["path"] for item in document["files"]}
        used = set()

        def visit(value):
            if isinstance(value, list):
                return [visit(item) for item in value]
            if not isinstance(value, dict):
                return value
            if MARKER in value:
                if set(value) != {MARKER} or not isinstance(value[MARKER], str) or value[MARKER] not in allowed:
                    raise ArtifactError("Candidate references a file outside its manifest")
                used.add(value[MARKER])
                return str(directory / value[MARKER])
            for key, item in value.items():
                if key in FILE_FIELDS and item is not None:
                    if not isinstance(item, dict) or set(item) != {MARKER}:
                        raise ArtifactError("Candidate contains an unbound file reference")
                if key == "artifact_paths" and (not isinstance(item, list) or any(
                        not isinstance(path, dict) or set(path) != {MARKER} for path in item)):
                    raise ArtifactError("Candidate artifact list contains an unbound file")
            return {key: visit(item) for key, item in value.items()}

        result = visit(encoded)
        if used != allowed:
            raise ArtifactError("Manifest contains unreferenced candidate files")
        return result

    def manifests(self, run, receipts):
        context = run_context(run)
        if not isinstance(receipts, dict) or set(receipts) != BRANCHES:
            raise ArtifactError("Finalize requires receipts for tree, lstm and gru")
        documents, total = {}, 0
        for branch in sorted(BRANCHES):
            receipt = receipts[branch]
            if not isinstance(receipt, dict) or any(receipt.get(key) != context[key] for key in ("region_id", "run_id")) or receipt.get("branch") != branch:
                raise ArtifactError("Receipt belongs to a different training context")
            document = self.store.load_manifest(receipt)
            envelope = document["result"]
            if (set(envelope) != {"schema", "context", "branch_result"} or type(envelope["schema"]) is not int
                    or envelope["schema"] != 1 or envelope["context"] != context):
                raise ArtifactError("Training dataset/configuration differs from manifest")
            # Validate branch semantics before downloading any file.
            check_result(run, branch, envelope["branch_result"])
            self.decode(envelope["branch_result"], document, Path("/manifest-validation-only"))
            total += sum(item["size"] for item in document["files"])
            if total > self.max_receive_bytes:
                raise ArtifactError("Combined training artifacts exceed receive budget")
            documents[branch] = document
        return documents

    @contextmanager
    def finalize_scope(self, run, receipts):
        documents = self.manifests(run, receipts)
        # Temporary data is owned by this scope and remains present through the
        # original registry call. It is released even when finalize raises.
        with tempfile.TemporaryDirectory(prefix="finalize-", dir=self.receive_root) as directory:
            root = Path(directory)
            results = {}
            for branch in sorted(BRANCHES):
                target = root / branch
                restored = self.store.restore(receipts[branch], target)
                if restored != documents[branch]:
                    raise ArtifactError("Manifest changed while receiving branch")
                results[branch] = self.decode(restored["result"]["branch_result"], restored, target)
                check_result(run, branch, results[branch])
            token = self.scope.set({"context": run_context(run), "results": results, "root": root})
            try:
                yield results
            finally:
                self.scope.reset(token)

    def read_branch(self, run, branch):
        scope = self.scope.get()
        if scope is None or scope["context"] != run_context(run) or branch not in BRANCHES:
            raise ArtifactError("Branch read requires matching finalize receipts")
        return copy.deepcopy(scope["results"][branch])

    def prepare_candidate(self, model_info):
        scope = self.scope.get()
        if scope is None:
            raise ArtifactError("Registration requires a verified finalize scope")
        context = scope["context"]
        commit = self.serving_root / context["region_id"] / context["run_id"] / str(uuid.uuid4())
        commit.mkdir(parents=True, exist_ok=False)
        copied = {}

        def convert(filename):
            if not isinstance(filename, str):
                raise ArtifactError("Invalid registration file reference")
            source = Path(filename).resolve(strict=True)
            if not source.is_relative_to(scope["root"]) or not source.is_file():
                raise ArtifactError("Registration file is outside verified branch results")
            target = commit / source.relative_to(scope["root"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if str(source) not in copied:
                shutil.copyfile(source, target)
                copied[str(source)] = str(target)
            return str(target)

        result = transform_paths(model_info, convert)
        if result.get("mlflow_model_uri") in copied:
            result["mlflow_model_uri"] = copied[result["mlflow_model_uri"]]
        return result, commit

    def publish_serving(self, directory):
        directory = Path(directory).resolve(strict=True)
        if not directory.is_relative_to(self.serving_root) or directory == self.serving_root:
            raise ArtifactError("Invalid serving publication directory")
        files = sorted(path for path in directory.rglob("*") if path.is_file())
        if not files or len(files) > self.store.max_files or sum(path.stat().st_size for path in files) > self.store.max_total_bytes:
            raise ArtifactError("Serving files exceed publication budget")
        prefix = "models/" if self.store.prefix == "training-exchange/v1" else self.store.prefix + "/serving/"
        for path in files:
            if any(part.is_symlink() for part in (path, *path.parents)):
                raise ArtifactError("Symlink serving file is not allowed")
            key = prefix + path.relative_to(self.model_root).as_posix()
            digest, size = file_digest(path), path.stat().st_size
            with path.open("rb") as source:
                self.store.client.put_object(Bucket=self.store.bucket, Key=key, Body=source, ContentLength=size,
                                             Metadata={"sha256": digest}, ContentType="application/octet-stream")
            head = self.store.client.head_object(Bucket=self.store.bucket, Key=key)
            if (head.get("ContentLength") != size or head.get("Metadata", {}).get("sha256") != digest
                    or path.stat().st_size != size or file_digest(path) != digest):
                raise ArtifactError("Serving publication validation failed")
            response = self.store.client.get_object(Bucket=self.store.bucket, Key=key)
            received, downloaded = hashlib.sha256(), 0
            with closing(response["Body"]) as body:
                while chunk := body.read(CHUNK):
                    downloaded += len(chunk)
                    if downloaded > size:
                        raise ArtifactError("Published serving file exceeds expected size")
                    received.update(chunk)
            if downloaded != size or received.hexdigest() != digest:
                raise ArtifactError("Published serving file checksum differs")

    def install(self, training, registry):
        if getattr(training, "_rke2_result_bridge", None) is not None:
            raise ArtifactError("Training result bridge is already installed")
        original_xcom = training._branch_xcom_response
        original_register = training.register_training_result
        original_copy = registry._copy_versioned_artifact

        def write(run, branch, result):
            self.published.set(None)
            receipt = self.publish_branch(run, branch, result)
            self.published.set((run_context(run), branch, receipt))

        def xcom(run, result):
            current = self.published.get()
            if current is None or current[:2] != (run_context(run), result.get("branch")):
                raise ArtifactError("Branch response requires a published artifact receipt")
            response = original_xcom(run, result)
            return {**response, "artifact_receipt": copy.deepcopy(current[2])}

        def copy_and_publish(info, new_version):
            directory = self.registration.get()
            if directory is None:
                raise ArtifactError("Registry publication scope is missing")
            original_copy(info, new_version)
            self.publish_serving(directory)

        def register(db, region_id, dataset_id, info, training_run_id=None):
            scope = self.scope.get()
            if scope is None or (str(region_id), str(dataset_id), str(training_run_id)) != tuple(
                    scope["context"][key] for key in ("region_id", "dataset_id", "run_id")):
                raise ArtifactError("Registry training context differs")
            prepared, directory = self.prepare_candidate(info)
            token = self.registration.set(directory)
            try:
                # The verified original helper copies/version-renames before
                # inserting the ModelVersion and committing. Its copy hook now
                # publishes all required files before that commit can succeed.
                return original_register(db, region_id, dataset_id, prepared, training_run_id=training_run_id)
            finally:
                self.registration.reset(token)

        training._write_branch_result = write
        training._branch_xcom_response = xcom
        training._read_branch_result = self.read_branch
        training._read_optional_branch_result = self.read_branch
        training.register_training_result = register
        registry._copy_versioned_artifact = copy_and_publish
        training._rke2_result_bridge = self
