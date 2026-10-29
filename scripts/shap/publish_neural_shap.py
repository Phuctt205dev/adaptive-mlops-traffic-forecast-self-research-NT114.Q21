"""Prepare a reviewed Pages build; preview by default, operator-only --publish."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.cloud_training.prepare_frontend import validate_targets
from scripts.shap.rollout_neural_shap import NeuralRollout, read

FILES = (
    "backend/app/schemas/feature_importance.py", "backend/app/services/feature_importance.py",
    "backend/app/services/training_control.py", "frontend/src/FeatureImportancePanel.jsx",
    "scripts/cloud_training/execute_branch.py", "scripts/cloud_training/prepare_rollout.py",
    "src/shap_reporting.py", "src/time_series_cross_validation.py",
    "tests/test_feature_importance.py", "tests/test_shap_reporting.py",
    "tests/test_neural_shap_integration.py", "tests/test_neural_shap_rollout.py", "tests/test_neural_shap_publish.py",
    "docs/guides/FEATURE_IMPORTANCE.md", "docs/guides/SHAP_INTEGRATION.md", "docs/guides/SHAP_NEURAL_ROLLOUT.md",
    "scripts/shap/check_neural_compatibility.py", "scripts/shap/check_neural_kaggle.py",
    "scripts/shap/check_neural_runtime.py", "scripts/shap/rollout_neural_shap.py", "scripts/shap/publish_neural_shap.py",
)


def git(*args):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, encoding="utf-8", timeout=120)
    if result.returncode:
        raise RuntimeError("Git command failed; inspect local Git state before retrying: " + args[0])
    return result.stdout.strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hashes():
    paths = set(FILES) | {path.relative_to(ROOT).as_posix() for path in (ROOT / "frontend/src").rglob("*") if path.is_file()}
    paths.update(("frontend/package.json", "frontend/package-lock.json", "frontend/vite.config.js", "frontend/index.html",
                  ".github/workflows/deploy-frontend-pages.yml"))
    return {name: sha(ROOT / name) for name in sorted(paths)}


def changed_files():
    changed = set(git("diff", "--name-only", "HEAD").splitlines())
    changed.update(git("ls-files", "--others", "--exclude-standard").splitlines())
    if not changed or not changed <= set(FILES):
        raise RuntimeError("Working changes differ from the reviewed neural SHAP publish scope.")
    return sorted(changed)


def prepare(directory, api_base_url, base_path):
    validate_targets(api_base_url, base_path)
    directory = Path(directory)
    if (directory / "neural-frontend-build.json").exists():
        raise RuntimeError("Keep an existing frontend review unchanged; prepare a fresh review after source changes.")
    if git("diff", "--cached", "--name-only"):
        raise RuntimeError("Other work is already staged.")
    before = hashes()
    environment = {**os.environ, "VITE_API_BASE_URL": api_base_url, "VITE_BASE_PATH": base_path}
    command = "npm.cmd" if sys.platform == "win32" else "npm"
    subprocess.run([command, "run", "build"], cwd=ROOT / "frontend", env=environment, check=True, timeout=120)
    if before != hashes():
        raise RuntimeError("Source changed during build; prepare a fresh review.")
    archive = directory / "neural-frontend-dist.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for path in (ROOT / "frontend/dist").rglob("*"):
            if path.is_file(): target.write(path, path.relative_to(ROOT / "frontend/dist").as_posix())
    review = {"bundle_id": read(directory / "review.json")["bundle_id"], "build_passed": True,
              "api_base_url": api_base_url, "base_path": base_path, "sources": before,
              "publish_files": changed_files(), "git_head": git("rev-parse", "HEAD"),
              "branch": git("branch", "--show-current"),
              "origin_sha256": hashlib.sha256(git("remote", "get-url", "origin").encode()).hexdigest(),
              "dist_sha256": sha(archive)}
    (directory / "neural-frontend-build.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    return {"frontend_prepared": True, "bundle_id": review["bundle_id"], "published": False, "publish_files": review["publish_files"]}


def publish(directory, *, do_publish=False, kubectl=None):
    directory = Path(directory)
    review = read(directory / "neural-frontend-build.json")
    validate_targets(review["api_base_url"], review["base_path"])
    if (not review.get("build_passed") or review["bundle_id"] != read(directory / "review.json")["bundle_id"]
            or review["sources"] != hashes() or sha(directory / "neural-frontend-dist.zip") != review["dist_sha256"]
            or changed_files() != review["publish_files"]):
        raise RuntimeError("Frontend source/archive/publish scope changed after prepare.")
    if (review["branch"] not in {"master", "main"} or git("branch", "--show-current") != review["branch"]
            or git("rev-parse", "HEAD") != review["git_head"]
            or hashlib.sha256(git("remote", "get-url", "origin").encode()).hexdigest() != review["origin_sha256"]):
        raise RuntimeError("Git branch, HEAD or origin changed after prepare.")
    if git("diff", "--cached", "--name-only"):
        raise RuntimeError("Other work is already staged.")
    if not do_publish:
        return {"neural_frontend_preview_verified": True, "published": False, "git_changed": False,
                "publish_files": review["publish_files"]}
    if not kubectl or not read(directory / "neural-rollout-journal.json").get("applied"):
        raise RuntimeError("Apply the reviewed backend before publishing.")
    NeuralRollout(kubectl, directory).verify()
    request = Request(review["api_base_url"] + "/health/cloud-training", headers={"ngrok-skip-browser-warning": "true"})
    with urlopen(request, timeout=20) as response: health = json.loads(response.read(65536))
    if health.get("bundle_id") != review["bundle_id"] or health.get("neural_shap_supported") is not True:
        raise RuntimeError("The public API does not serve the reviewed neural SHAP bundle.")
    objects = {name: git("hash-object", "--path=" + name, name) for name in review["publish_files"]}
    git("add", "--", *review["publish_files"])
    if set(git("diff", "--cached", "--name-only").splitlines()) != set(review["publish_files"]):
        raise RuntimeError("The staged list differs; nothing committed or pushed.")
    if any(git("rev-parse", ":" + name) != wanted for name, wanted in objects.items()):
        raise RuntimeError("Staged content differs; nothing committed or pushed.")
    git("commit", "-m", "Add SHAP explanations for LSTM and GRU")
    proof = {"commit": git("rev-parse", "HEAD"), "bundle_id": review["bundle_id"], "push_confirmed": False,
             "checked_at": datetime.now(timezone.utc).isoformat()}
    path = directory / "neural-publish.json"
    path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
    git("push", "origin", review["branch"])
    proof["push_confirmed"] = True
    path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
    return {"neural_source_pushed": True, **proof, "pages_workflow_confirmation_pending": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--api-base-url")
    parser.add_argument("--base-path")
    parser.add_argument("--kubectl")
    args = parser.parse_args()
    if args.prepare and args.publish: parser.error("Prepare and publish are separate actions.")
    try:
        result = prepare(args.directory, args.api_base_url, args.base_path) if args.prepare else publish(
            args.directory, do_publish=args.publish, kubectl=args.kubectl)
        print(json.dumps(result)); return 0
    except Exception as error:
        print(json.dumps({"neural_publish_stopped": True, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Inspect reviewed files; raw details omitted."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
