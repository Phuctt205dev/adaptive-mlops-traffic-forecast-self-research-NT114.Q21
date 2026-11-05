"""Validate CI provenance and the real production approval gate, hosted only."""
import argparse
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def get(path):
    token = os.environ["GH_TOKEN"]
    request = Request("https://api.github.com/" + path, headers={"Authorization": "Bearer " + token,
                      "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError("GitHub API check failed (HTTP " + str(error.code) + "); token/body omitted.") from None


def validate(repository, run_id, *, fetch=get):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not str(run_id).isdigit():
        raise ValueError("Invalid repository or workflow run identity.")
    run = fetch(f"repos/{repository}/actions/runs/{run_id}")
    workflow = fetch(f"repos/{repository}/actions/workflows/backend-ci.yml")
    repo = fetch(f"repos/{repository}")
    if (run.get("conclusion") != "success" or run.get("status") != "completed" or run.get("event") != "push"
            or run.get("workflow_id") != workflow["id"] or run.get("head_branch") != repo["default_branch"]
            or (run.get("head_repository") or {}).get("full_name", "").lower() != repository.lower()
            or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))):
        raise ValueError("Only successful default-branch push runs from this repository may deploy.")
    environment = fetch(f"repos/{repository}/environments/rke2-production")
    rules = environment.get("protection_rules", [])
    if not any(r.get("type") == "required_reviewers" and r.get("reviewers") for r in rules):
        raise ValueError("rke2-production must have required reviewers; deployment stays disabled.")
    return {"sha": run["head_sha"], "run_id": str(run_id), "artifact": "backend-release-" + run["head_sha"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = validate(os.environ["GITHUB_REPOSITORY"], args.run_id)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        for key, value in result.items():
            stream.write(key + "=" + value + "\n")
    print(json.dumps({"ci_provenance_verified": True, "approval_gate_verified": True, **result}))


if __name__ == "__main__":
    main()
