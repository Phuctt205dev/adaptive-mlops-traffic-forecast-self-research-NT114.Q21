"""Build and hash the frontend for operator review; never stage, commit or push."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PUBLISH_FILES = ["frontend/src/api.js", "frontend/src/main.jsx", "frontend/src/styles.css", "frontend/src/TrainingMonitor.jsx"]


def git(*args):
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=40)
    if result.returncode:
        raise RuntimeError("Git inspection failed; raw details omitted.")
    return result.stdout.strip()


def validate_targets(api_base_url, base_path):
    url = urlparse(api_base_url)
    if (url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment
            or url.path != "/api/v1" or url.params):
        raise ValueError("Use the public HTTPS API URL ending in /api/v1, without credentials or query parameters.")
    if (not base_path.startswith("/") or not base_path.endswith("/") or ".." in base_path
            or any(char in base_path for char in "\\?#%\r\n")):
        raise ValueError("Use an absolute Pages base path with a trailing slash.")


def source_hashes():
    paths = [*(ROOT / "frontend/src").rglob("*"),
             *(ROOT / "frontend" / name for name in ("package.json", "package-lock.json", "index.html", "vite.config.js")),
             ROOT / ".github/workflows/deploy-frontend-pages.yml"]
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths) if path.is_file()}


def prepare(review, api_base_url, base_path):
    validate_targets(api_base_url, base_path)
    directory = Path(review).resolve()
    identity = json.loads((directory / "review.json").read_text(encoding="utf-8"))
    if (directory / "frontend-build.json").exists():
        raise RuntimeError("A frontend review already exists. Keep it unchanged; prepare another directory for a new build.")
    before = source_hashes()
    env = {**os.environ, "VITE_API_BASE_URL": api_base_url, "VITE_BASE_PATH": base_path}
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise RuntimeError("npm is not installed.")
    result = subprocess.run([npm, "run", "build"], cwd=ROOT / "frontend", env=env,
                            text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=120)
    (directory / "frontend-build.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode or before != source_hashes():
        raise RuntimeError("Frontend build failed or its source changed during the build. Review frontend-build.log.")
    archive = directory / "frontend-dist.zip"
    dist = ROOT / "frontend/dist"
    files = sorted(path for path in dist.rglob("*") if path.is_file())
    if not files or any(path.is_symlink() or not path.resolve().is_relative_to(dist.resolve()) for path in files):
        raise RuntimeError("Frontend output is missing or contains unsafe paths.")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in files:
            output.write(path, path.relative_to(dist).as_posix())
    proof = {"bundle_id": identity["bundle_id"], "api_base_url": api_base_url, "base_path": base_path,
             "source_files": before, "publish_files": PUBLISH_FILES, "git_head": git("rev-parse", "HEAD"),
             "branch": git("branch", "--show-current"),
             "origin_sha256": hashlib.sha256(git("remote", "get-url", "origin").encode()).hexdigest(),
             "dist_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "build_passed": True, "published": False}
    (directory / "frontend-build.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    return {"frontend_prepared": True, "api_base_url": api_base_url, "base_path": base_path, "published": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True)
    parser.add_argument("--api-base-url", required=True)
    parser.add_argument("--base-path", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.review, args.api_base_url, args.base_path)))
    except Exception as error:
        print(json.dumps({"frontend_prepared": False, "error_type": type(error).__name__,
                          "safe_message": str(error) if isinstance(error, (RuntimeError, ValueError)) else "Build review failed; raw details omitted."}))
        raise SystemExit(2)
