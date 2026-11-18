"""One model per bounded Job, common portable artifacts, no local ML imports."""
import copy
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import time

from src.cloud_training.artifacts import MAX_BYTES, MAX_FILES, read_bundle, relative_path
from src.cloud_training.kaggle_execution import TrainingCancelled
from src.cloud_training.package import validate_context
from src.cloud_training.probe import canonical, sha256_file
from src.cloud_training.process import PREFIX
from src.cloud_training.telemetry import operation_timing
from src.local_training.kubernetes import Kubernetes, MODELS, identity, manifest


def initial_ledger(snapshot):
    validate_context(snapshot["context"])
    return {"backend": "local", "snapshot": copy.deepcopy(snapshot), "jobs": {},
            "phase": "prepare_input", "telemetry": {}, "remote_stopped": False, "sequence": 0}


class LocalExecution:
    def __init__(self, store, directory, ledger, save, settings, *, client=None, sleep=time.sleep, clock=time.monotonic, admission=None):
        self.store, self.directory, self.ledger, self.save_callback = store, Path(directory), ledger, save
        self.settings, self.client = settings, client or Kubernetes(settings.local_training_namespace)
        self.sleep, self.clock = sleep, clock
        self.admission = admission or (lambda: nullcontext(True))
        context = ledger["snapshot"]["context"]
        validate_context(context)
        self.prefix = f"local-training/v1/{context['run_id']}/{context['branch']}/{context['attempt_id']}/"

    def save(self, **values):
        self.ledger.update(values)
        self.save_callback(copy.deepcopy(self.ledger))

    def phase(self, name):
        if self.ledger.get("phase") != name:
            self.save(phase=name)

    @contextmanager
    def measured(self, name):
        start = self.clock()
        try:
            yield
        finally:
            telemetry = copy.deepcopy(self.ledger.get("telemetry", {}))
            operation_timing(telemetry, name, self.clock() - start)
            self.save(telemetry=telemetry)

    def verify(self, current, desired):
        if current is None:
            raise RuntimeError("The local training Job disappeared before stop was confirmed.")
        if (current["metadata"].get("annotations", {}).get("traffic.mlops/identity") != desired["metadata"]["annotations"]["traffic.mlops/identity"]
                or current["spec"]["template"]["spec"]["containers"][0]["image"] != desired["spec"]["template"]["spec"]["containers"][0]["image"]):
            raise RuntimeError("Local training Job identity differs.")
        return current

    def upload(self):
        for name, descriptor in self.ledger["snapshot"]["files"].items():
            path = self.directory / "input" / relative_path(name)
            if path.stat().st_size != descriptor["size"] or sha256_file(path) != descriptor["sha256"]:
                raise RuntimeError("Local input snapshot changed.")
            self.store.client.upload_file(str(path), self.store.bucket, self.prefix + "input/" + name)
        self.store.client.put_object(Bucket=self.store.bucket, Key=self.prefix + "snapshot.json", Body=canonical(self.ledger["snapshot"]))

    def receive(self, name, context):
        target = self.directory / "output" / name
        target.mkdir(parents=True, exist_ok=False)
        prefix = self.prefix + "output/" + name + "/"
        document = self.store.client.get_object(Bucket=self.store.bucket, Key=prefix + "manifest.json")
        with document["Body"] as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise RuntimeError("Local output manifest exceeds its byte budget.")
        metadata = json.loads(raw)
        files = metadata.get("files")
        if metadata.get("context") != context or not isinstance(files, list) or len(files) > MAX_FILES:
            raise RuntimeError("Local output manifest identity differs.")
        total = 0
        names = set()
        for item in files:
            path = relative_path(item["path"])
            if type(item.get("size")) is not int or item["size"] < 1 or path in names:
                raise RuntimeError("Invalid local output descriptor.")
            names.add(path)
            total += item["size"]
            if total > MAX_BYTES:
                raise RuntimeError("Local output exceeds its byte budget.")
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            response = self.store.client.get_object(Bucket=self.store.bucket, Key=prefix + path)
            with response["Body"] as source, destination.open("xb") as output:
                received = 0
                while chunk := source.read(1024 * 1024):
                    received += len(chunk)
                    if received > item["size"]:
                        raise RuntimeError("Local output exceeded its declared size.")
                    output.write(chunk)
        (target / "manifest.json").write_bytes(raw)
        return read_bundle(target, context)

    def drain(self, name, model, progress):
        entry = self.ledger["jobs"][name]
        for pod in self.client.pods(name):
            if pod.get("status", {}).get("phase") in {None, "Pending"}:
                continue
            for line in self.client.logs(pod).splitlines():
                if not line.startswith(PREFIX):
                    continue
                if len(line) > 16384:
                    raise RuntimeError("Local progress exceeds its byte budget.")
                document = json.loads(line[len(PREFIX):])
                if document.get("context") != self.ledger["snapshot"]["context"]:
                    raise RuntimeError("Local progress identity differs.")
                event = document["event"]
                if "control" in event:
                    continue
                if event.get("model") != model or type(event.get("sequence")) is not int:
                    raise RuntimeError("Local progress model differs.")
                if event["sequence"] <= entry.get("last_sequence", 0):
                    continue
                emitted_sequence = event["sequence"]
                event = {**event, "sequence": self.ledger.get("sequence", 0) + 1,
                         "source_clock": "local_" + identity(self.ledger["snapshot"]["context"], model)[:24]}
                progress(event)
                entry["last_sequence"] = emitted_sequence
                self.save(sequence=event["sequence"])

    def run(self, progress, cancelled):
        context = self.ledger["snapshot"]["context"]
        config = json.loads((self.directory / "input/job.json").read_text(encoding="utf-8"))["configuration"]
        selected = [m for m in MODELS if m in config["selected_models"] and (context["branch"] == "tree" if m.endswith("_lag") else context["branch"] == m)]
        if cancelled():
            raise TrainingCancelled("Local training stop requested before input upload.")
        self.upload()
        results = []
        deadline = self.clock() + self.settings.local_training_deadline_seconds
        for model in selected:
            desired = manifest(self.settings, context, model, self.prefix, self.store.bucket)
            name = desired["metadata"]["name"]
            self.ledger["jobs"].setdefault(name, {"desired": desired, "model": model, "last_sequence": 0})
            self.save()
            while True:
                if cancelled():
                    raise TrainingCancelled("Local training stop requested.")
                if self.clock() >= deadline:
                    raise RuntimeError("Local training exceeded its deadline.")
                current = self.client.get(name)
                if current is None:
                    if self.ledger["jobs"][name].get("uid"):
                        raise RuntimeError("Local training Job was lost; it will not be silently retried.")
                    self.phase("waiting_resources")
                    with self.admission() as admitted:
                        current = self.client.get(name) if admitted else None
                        eligible = self.client.available(self.settings.local_training_memory_mib, self.settings.local_training_reserve_mib,
                                                         self.settings.local_training_cpu) if admitted and current is None else []
                        if eligible:
                            # Reserve one eligible node while holding a cross-worker
                            # admission lock. Pending Jobs count as reservations too.
                            terms = desired["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
                            terms[0]["matchExpressions"][0]["values"] = eligible[:1]
                            self.save()
                            if cancelled():
                                raise TrainingCancelled("Local training stop requested during admission.")
                            current = self.client.create(desired)
                    if current is None:
                        self.sleep(3)
                        continue
                current = self.verify(current, desired)
                self.ledger["jobs"][name]["uid"] = current["metadata"]["uid"]
                self.save()
                pods = self.client.pods(name)
                self.phase("local_running" if any(p.get("status", {}).get("phase") == "Running" for p in pods) else "waiting_resources")
                self.drain(name, model, progress)
                conditions = {c["type"]: c["status"] for c in current.get("status", {}).get("conditions", [])}
                if conditions.get("Failed") == "True":
                    self.save(last_failure={"category": "local_job_failed", "operation": "local_training"})
                    raise RuntimeError("Local training Job failed. Check its resource limits and input.")
                if conditions.get("Complete") == "True":
                    self.phase("output_download")
                    results.append(self.receive(name, context))
                    retirement = self.clock() + 90
                    while not self.client.stop(name, current["metadata"]["uid"]):
                        if cancelled():
                            raise TrainingCancelled("Local training stop requested.")
                        if self.clock() >= retirement:
                            raise RuntimeError("The completed training Job has not released its pod.")
                        self.sleep(2)
                    self.ledger["jobs"][name]["deleted"] = True
                    self.save()
                    break
                self.sleep(3)
        candidates = [c for result in results for c in result.get("candidates", [])]
        if not candidates:
            raise RuntimeError("Local training returned no candidates.")
        # Match pipeline.py exactly: unrounded mean CV MAE, stable algorithm
        # order on a tie. Display rounding must never change the winner.
        best = min(candidates, key=lambda c: float(c.get("selection_cv_mae", c["validation_MAE"])))
        if context["branch"] == "tree":
            # Preserve the existing tree-branch contract: one winner with all selected reports.
            best["shap_reports"] = {k: v for c in candidates for k, v in c.get("shap_reports", {}).items()}
            best["model_comparison"] = [row for c in candidates for row in c.get("model_comparison", [])]
            candidates = [best]
        self.save(remote_stopped=True)
        return {"branch": context["branch"], "status": "completed", "candidates": candidates, "production_candidate": best}

    def stop(self):
        stopped = True
        for name, entry in self.ledger.get("jobs", {}).items():
            current = self.client.get(name)
            if current is not None:
                self.verify(current, entry["desired"])
                if not self.client.stop(name, current["metadata"]["uid"]):
                    stopped = False
            elif self.client.pods(name):
                stopped = False
        if stopped:
            self.save(remote_stopped=True)
        return stopped

    def cleanup(self):
        if not self.stop():
            return False
        # Import here to avoid a module cycle. Scope is derived from canonical UUIDs.
        from backend.app.services.training_control import delete_prefix
        clean = delete_prefix(self.store, self.prefix)
        if clean:
            self.save(staging_cleaned=True)
        return clean
