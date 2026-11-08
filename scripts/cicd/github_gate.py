"""Validate immutable CI provenance and the selected deployment policy, hosted only."""
import argparse
import json
import os
from pathlib import Path
import re
import base64
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.cicd.cd_state import BRANCH, FILE, ENVIRONMENTS, initial, validate as validate_state
from scripts.cicd.ci_coverage import check_run


class MissingResource(RuntimeError):
    pass


def get(path):
    token = os.environ["GH_TOKEN"]
    request = Request("https://api.github.com/" + path, headers={"Authorization": "Bearer " + token,
                      "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                      'Cache-Control': 'no-cache'})
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('GitHub response exceeds its budget.')
            return json.loads(raw)
    except HTTPError as error:
        if error.code == 404:
            raise MissingResource('GitHub resource is missing.') from None
        raise RuntimeError("GitHub API check failed (HTTP " + str(error.code) + "); token/body omitted.") from None


def validate(repository, run_id, *, fetch=get, operation='release', coverage=check_run):
    if operation not in {'release','migrate-role-runtime-v1'}:
        raise ValueError('Unsupported deployment operation')
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
    controller = fetch(f'repos/{repository}/contents/scripts/cicd/cd_state.py?ref={run["head_sha"]}')
    if controller.get('type') != 'file':
        raise ValueError('This CI does not contain the mode-aware delivery controller.')
    runtime=fetch(f'repos/{repository}/contents/deployment/rke2/runtime-contract.json?ref={run["head_sha"]}')
    if (runtime.get('type')!='file' or runtime.get('encoding')!='base64'
            or json.loads(base64.b64decode(''.join(runtime.get('content','').split()),validate=True)).get('schema')!='traffic-runtime-contract/v1'):
        raise ValueError('This CI predates the unified runtime delivery contract; use its original controller for recovery only.')
    try:
        config = fetch(f'repos/{repository}/contents/{FILE}?ref={BRANCH}')
        raw = base64.b64decode(''.join(config['content'].split()), validate=True)
        if config.get('type') != 'file' or config.get('encoding') != 'base64' or len(raw) > 32768:
            raise ValueError('Invalid CD configuration envelope.')
        state = validate_state(json.loads(raw))
    except MissingResource:
        state = initial()
    mode = state['mode']
    if operation!='release' and mode!='manual':
        raise ValueError('Runtime migration requires the reviewed Manual gate')
    if state['active'] is not None:
        raise ValueError('An earlier delivery needs completion or verified recovery.')
    if mode == 'auto' and state['blocked']:
        raise ValueError('Auto is blocked by a failed release; use Manual to verify a repair.')
    environment_name = ENVIRONMENTS[mode]
    environment = fetch(f"repos/{repository}/environments/{environment_name}")
    rules = environment.get("protection_rules", [])
    reviewers = any(r.get("type") == "required_reviewers" and r.get("reviewers") for r in rules)
    if mode == 'manual' and not reviewers:
        raise ValueError("rke2-production must have required reviewers; deployment stays disabled.")
    if mode == 'auto' and reviewers:
        raise ValueError('Auto environment unexpectedly requires review; policy was not changed automatically.')
    policy = environment.get('deployment_branch_policy') or {}
    branches = fetch(f'repos/{repository}/environments/{environment_name}/deployment-branch-policies')
    if (not policy.get('custom_branch_policies') or policy.get('protected_branches')
            or [(p.get('name'), p.get('type')) for p in branches.get('branch_policies', [])] != [(repo['default_branch'], 'branch')]):
        raise ValueError('Deployment environment must allow only the default branch.')
    if type(run.get('run_number')) is not int or run['run_number'] < 1:
        raise ValueError('CI sequence is missing.')
    if str(run.get('id'))!=str(run_id) or type(run.get('run_attempt')) is not int or run['run_attempt']<1:
        raise ValueError('CI run attempt identity is missing.')
    proof=coverage(repository,run,fetch)
    if operation!='release' and proof['scope']['full'] is not True:
        raise ValueError('Runtime migration requires full CI coverage')
    return {"sha": run["head_sha"], "run_id": str(run_id), "artifact": "backend-release-" + run["head_sha"],
            'mode': mode, 'epoch': str(state['epoch']), 'environment': environment_name, 'ci_number': str(run['run_number']),
            'operation':operation}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = validate(os.environ["GITHUB_REPOSITORY"], args.run_id,operation=os.environ.get('DELIVERY_OPERATION','release'))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        for key, value in result.items():
            stream.write(key + "=" + value + "\n")
    print(json.dumps({"ci_provenance_verified": True, "deployment_policy_verified": True, **result}))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
            stream.write('## Deployment selection\n\n- Mode: **' + result['mode'] + '**\n- Environment: `' +
                         result['environment'] + '`\n- Commit: `' + result['sha'] + '`\n- Source CI: https://github.com/' +
                         os.environ['GITHUB_REPOSITORY'] + '/actions/runs/' + result['run_id'] + '\n')


if __name__ == "__main__":
    main()
