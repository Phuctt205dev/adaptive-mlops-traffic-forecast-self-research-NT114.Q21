"""One-module model-delete fix. Check is read-only; the operator applies/publishes."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.shap import rollout_neural_shap as base
from scripts.shap.publish_neural_shap import git, sha
from scripts.cloud_training.prepare_rollout import canonical, code_map, sources
from scripts.cloud_training.snapshot_cluster import capture
from scripts.cloud_training.manage_rollout import Rollout

PRIOR = ROOT / "results/shap-neural/review-v2"
MODULE = "project/backend/app/services/model_registry.py"
FILES = ("backend/app/services/model_registry.py", "frontend/src/main.jsx",
         "tests/test_model_deletion_constraints.py", "scripts/maintenance/model_delete_fix.py")


def source_patch(volume, code, items):
    replacement = copy.deepcopy(volume)
    replacement["configMap"].update(name=code["metadata"]["name"], items=items)
    return {"spec": {"template": {"spec": {"volumes": [replacement]}}}}


def prepare(kubectl, directory):
    directory = Path(directory)
    if directory.exists(): raise RuntimeError("Keep an existing review unchanged; use a new directory.")
    prior_resources = base.read(PRIOR / "configmaps.json")["items"]
    old_code = next(item for item in prior_resources if item["metadata"]["name"].startswith("traffic-cloud-")
                    and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
    old = base.code_manifest(old_code)
    code, items, manifest = code_map(sources())
    changed = {name for name in set(old["files"]) | set(manifest["files"]) if old["files"].get(name) != manifest["files"].get(name)}
    if changed != {MODULE}: raise RuntimeError("Only the model registry module may change in this fix.")
    cluster = Rollout(kubectl, base.PRIOR, base.PRIOR / "baseline.json")
    cluster.verify(expected=True, bundle_id=old["bundle_id"])
    baseline = capture(kubectl)
    documents = {"baseline.json": baseline, "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": [code,
        *[item for item in prior_resources if item["metadata"]["name"].startswith("traffic-cloud-airflow-")]]}}
    specifications = {}
    for name in base.ROLES:
        volume = next(item for item in baseline["deployments"][name]["volumes"] if item["name"] == "cloud-code")
        if volume["configMap"]["name"] != old_code["metadata"]["name"]:
            raise RuntimeError("Running cloud code differs from the reviewed baseline.")
        documents[name + ".patch.json"] = source_patch(volume, code, items)
        documents[name + ".rollback.json"] = {"spec": {"template": {"spec": {"volumes": [copy.deepcopy(volume)]}}}}
        before = cluster.call("get", "deployment", name, "-o", "json", json_result=True)
        proposed = cluster.call("patch", "deployment", name, "--type=strategic", "--dry-run=server", "-o", "json", "--patch-file=-",
                               content=json.dumps(documents[name + ".patch.json"]), json_result=True)
        if before["metadata"]["generation"] != baseline["deployments"][name]["generation"]:
            raise RuntimeError("A deployment changed during prepare.")
        specifications[name] = {"before": hashlib.sha256(canonical(before["spec"])).hexdigest(),
                                "after": hashlib.sha256(canonical(proposed["spec"])).hexdigest()}
    documents["deployment-spec-hashes.json"] = specifications
    directory.mkdir(parents=True)
    review = {"bundle_id": manifest["bundle_id"], "baseline_bundle_id": old["bundle_id"], "changed_files": sorted(changed),
              "operator_sha256": sha(base.__file__), "preparer_sha256": sha(ROOT / "scripts/cloud_training/prepare_rollout.py"),
              "model_delete_operator_sha256": sha(__file__), "files": {}, "data_deleted": False}
    for name, value in documents.items():
        (directory / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
        review["files"][name] = sha(directory / name)
    (directory / "review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"prepared": True, "bundle_id": manifest["bundle_id"], "data_deleted": False}


class ModelDeleteFix(base.NeuralRollout):
    def __init__(self, kubectl, directory):
        super().__init__(kubectl, directory)
        if self.review.get("changed_files") != [MODULE] or self.review.get("model_delete_operator_sha256") != sha(__file__):
            raise RuntimeError("Model-delete fix scope or operator changed.")
        self.journal_path = self.directory / "model-delete-rollout-journal.json"
        self.journal = base.read(self.journal_path) if self.journal_path.exists() else {"bundle_id": self.review["bundle_id"]}
        if self.journal["bundle_id"] != self.review["bundle_id"]: raise RuntimeError("Wrong rollout journal.")

    def check(self):
        result = self.validate()
        for name in base.ROLES:
            proof = base.read(self.directory / (name + ".source-proof.json"))
            if proof.get("source_startup_verified") is not True or proof.get("bundle_id") != self.review["bundle_id"]:
                raise RuntimeError("The exact fix needs startup proofs for all three roles.")
        return {**result, "model_delete_fix_ready_to_apply": True, "data_deleted": False}

    def apply(self):
        result = super().apply()
        result.pop("neural_shap_applied", None)
        return {**result, "model_delete_fix_applied": True, "data_deleted": False}


def frontend_sources():
    paths = set(FILES) | {p.relative_to(ROOT).as_posix() for p in (ROOT / "frontend/src").rglob("*") if p.is_file()}
    paths.update(("frontend/vite.config.js", "frontend/package.json", "frontend/package-lock.json", "frontend/index.html",
                  ".github/workflows/deploy-frontend-pages.yml"))
    return {name: sha(ROOT / name) for name in sorted(paths)}


def prepare_frontend(directory):
    directory = Path(directory)
    path = directory / "model-delete-frontend.json"
    if path.exists(): raise RuntimeError("Keep the frontend review unchanged; prepare a new package.")
    old = base.read(PRIOR / "neural-frontend-build.json")
    before = frontend_sources()
    subprocess.run(["npm.cmd" if sys.platform == "win32" else "npm", "run", "build"], cwd=ROOT / "frontend",
                   env={**os.environ, "VITE_API_BASE_URL": old["api_base_url"], "VITE_BASE_PATH": old["base_path"]},
                   check=True, timeout=120)
    if before != frontend_sources(): raise RuntimeError("Source changed during build.")
    review = {"sources": before, "bundle_id": base.read(directory / "review.json")["bundle_id"],
              "api_base_url": old["api_base_url"], "base_path": old["base_path"],
              "git_head": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
              "origin_sha256": hashlib.sha256(git("remote", "get-url", "origin").encode()).hexdigest()}
    path.write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"frontend_prepared": True, "published": False}


def publish(kubectl, directory, *, do_publish=False):
    directory = Path(directory); review = base.read(directory / "model-delete-frontend.json")
    changed = set(git("diff", "--name-only", "HEAD").splitlines()) | set(git("ls-files", "--others", "--exclude-standard").splitlines())
    if (review["sources"] != frontend_sources() or changed != set(FILES) or git("diff", "--cached", "--name-only")
            or review["git_head"] != git("rev-parse", "HEAD") or review["branch"] not in {"main", "master"}
            or review["branch"] != git("branch", "--show-current")
            or review["origin_sha256"] != hashlib.sha256(git("remote", "get-url", "origin").encode()).hexdigest()):
        raise RuntimeError("Source/Git differs from the reviewed four-file fix.")
    if not do_publish: return {"publish_preview_verified": True, "published": False, "files": FILES}
    operator = ModelDeleteFix(kubectl, directory)
    if not operator.journal.get("applied"): raise RuntimeError("Apply the backend first.")
    operator.verify()
    request = Request(review["api_base_url"] + "/health/cloud-training", headers={"ngrok-skip-browser-warning": "true"})
    with urlopen(request, timeout=20) as response: health = json.loads(response.read(65536))
    if health.get("bundle_id") != review["bundle_id"]: raise RuntimeError("Public API serves a different bundle.")
    objects = {name: git("hash-object", "--path=" + name, name) for name in FILES}
    git("add", "--", *FILES)
    if (set(git("diff", "--cached", "--name-only").splitlines()) != set(FILES)
            or any(git("rev-parse", ":" + name) != value for name, value in objects.items())):
        raise RuntimeError("Staged scope/content differs; nothing committed or pushed.")
    git("commit", "-m", "Fix model deletion with drift history and show delete errors")
    proof = {"commit": git("rev-parse", "HEAD"), "push_confirmed": False}
    path = directory / "model-delete-publish.json"
    path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
    git("push", "origin", review["branch"])
    proof["push_confirmed"] = True
    path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["Prepare", "Check", "Apply", "Verify", "Rollback", "PrepareFrontend", "Preview", "Publish"])
    parser.add_argument("--kubectl", default="C:/Users/ADMIN/AppData/Local/Programs/kubectl/kubectl.exe")
    parser.add_argument("--directory", default="results/model-delete-fix/review-v1")
    args = parser.parse_args()
    try:
        if args.action == "Prepare": result = prepare(args.kubectl, args.directory)
        elif args.action == "PrepareFrontend": result = prepare_frontend(args.directory)
        elif args.action in {"Preview", "Publish"}: result = publish(args.kubectl, args.directory, do_publish=args.action == "Publish")
        else: result = getattr(ModelDeleteFix(args.kubectl, args.directory), args.action.lower())()
        print(json.dumps(result)); return 0
    except Exception as error:
        print(json.dumps({"model_delete_fix_stopped": True, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect local review; raw details omitted."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
