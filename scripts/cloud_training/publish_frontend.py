"""Preview by default. Only the operator invokes --publish after enabling RKE2."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.request import Request, urlopen

try:
    from .prepare_frontend import PUBLISH_FILES, ROOT, git, source_hashes, validate_targets
except ImportError:
    from prepare_frontend import PUBLISH_FILES, ROOT, git, source_hashes, validate_targets


def checked_git(*args):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=90)
    if result.returncode:
        raise RuntimeError("Git " + args[0] + " failed. Inspect local Git state before retrying; raw details omitted.")


def publish(review, *, do_publish=False, kubectl=None, snapshot=None):
    directory = Path(review).resolve()
    identity = json.loads((directory / "review.json").read_text(encoding="utf-8"))
    build = json.loads((directory / "frontend-build.json").read_text(encoding="utf-8"))
    validate_targets(build["api_base_url"], build["base_path"])
    if (build.get("build_passed") is not True or build["bundle_id"] != identity["bundle_id"]
            or build["publish_files"] != PUBLISH_FILES or build["source_files"] != source_hashes()
            or hashlib.sha256((directory / "frontend-dist.zip").read_bytes()).hexdigest() != build["dist_sha256"]):
        raise RuntimeError("The reviewed frontend source/archive differs. Prepare a new review before publishing.")
    if (build["branch"] not in {"master", "main"} or git("branch", "--show-current") != build["branch"]
            or git("rev-parse", "HEAD") != build["git_head"]
            or hashlib.sha256(git("remote", "get-url", "origin").encode()).hexdigest() != build["origin_sha256"]):
        raise RuntimeError("Git branch, HEAD or origin changed after frontend preparation.")
    if git("diff", "--cached", "--name-only"):
        raise RuntimeError("Other work is already staged. Review it separately before this publish.")
    if not do_publish:
        return {"frontend_preview_verified": True, "published": False, "git_changed": False,
                "publish_files": PUBLISH_FILES, "api_base_url": build["api_base_url"], "base_path": build["base_path"]}
    if not kubectl or not snapshot:
        raise RuntimeError("--publish requires the reviewed kubectl/snapshot so the enabled cluster can be verified.")
    try:
        from .manage_rollout import Rollout
    except ImportError:
        from manage_rollout import Rollout
    verification = Rollout(kubectl, directory, snapshot).verify(expected=True)
    request = Request(build["api_base_url"] + "/health/cloud-training", headers={"ngrok-skip-browser-warning": "true"})
    with urlopen(request, timeout=20) as response:
        health = json.loads(response.read(65536))
    if health.get("bundle_id") != identity["bundle_id"] or health.get("cloud_enabled") is not True:
        raise RuntimeError("The public API does not serve the enabled reviewed cloud bundle.")
    checked_git("add", "--", *PUBLISH_FILES)
    staged = set(git("diff", "--cached", "--name-only").splitlines())
    if not staged or not staged.issubset(PUBLISH_FILES):
        raise RuntimeError("Staged changes differ from the frontend publish scope; nothing committed or pushed.")
    checked_git("commit", "-m", "Add manual Cloud Training selection and admin training monitor")
    commit = git("rev-parse", "HEAD")
    proof = {"bundle_id": identity["bundle_id"], "commit": commit, "push_confirmed": False,
             "cluster_verified": verification["rollout_verified"], "checked_at": datetime.now(timezone.utc).isoformat()}
    (directory / "frontend-publish.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    checked_git("push", "origin", build["branch"])
    proof["push_confirmed"] = True
    (directory / "frontend-publish.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    return {"frontend_source_pushed": True, "commit": commit, "pages_workflow_confirmation_pending": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--kubectl")
    parser.add_argument("--snapshot")
    args = parser.parse_args()
    try:
        print(json.dumps(publish(args.review, do_publish=args.publish, kubectl=args.kubectl, snapshot=args.snapshot)))
    except Exception as error:
        print(json.dumps({"frontend_publish_failed": True, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, RuntimeError) else "Publishing check failed; raw details omitted."}))
        raise SystemExit(2)
