"""Validate immutable CI provenance and the selected deployment policy, hosted only."""
import argparse
import json
import os
from pathlib import Path
import re
import base64
import ssl
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.cicd.cd_state import BRANCH, FILE, ENVIRONMENTS, initial, validate as validate_state
from scripts.cicd.ci_coverage import check_run


class MissingResource(RuntimeError):
    pass


class Superseded(ValueError):
    """A release not yet claimed was replaced. This is not a CD failure."""


def get(path):
    token = os.environ["GH_TOKEN"]
    request = Request("https://api.github.com/" + path, headers={"Authorization": "Bearer " + token,
                      "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                      'Cache-Control': 'no-cache'})
    try:
        context = ssl.create_default_context()
        if os.name == 'nt' and os.environ.get('NODE_EXTRA_CA_CERTS'):
            context.load_verify_locations(cafile=os.environ['NODE_EXTRA_CA_CERTS'])
        with urlopen(request, timeout=30, context=context) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('GitHub response exceeds its budget.')
            return json.loads(raw)
    except HTTPError as error:
        if error.code == 404:
            raise MissingResource('GitHub resource is missing.') from None
        raise RuntimeError("GitHub API check failed (HTTP " + str(error.code) + "); token/body omitted.") from None


def validate(repository, run_id, *, fetch=get, operation='release', coverage=check_run,
             expected_commit=None, expected_attempt=None, current_run=False):
    if operation not in {'release','migrate-role-runtime-v1'}:
        raise ValueError('Unsupported deployment operation')
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not str(run_id).isdigit():
        raise ValueError("Invalid repository or workflow run identity.")
    run = fetch(f"repos/{repository}/actions/runs/{run_id}")
    workflow = fetch(f"repos/{repository}/actions/workflows/backend-ci.yml")
    repo = fetch(f"repos/{repository}")
    terminal = run.get('status') == 'completed' and run.get('conclusion') == 'success'
    if current_run:
        terminal = same_run(run, run_id, expected_commit, expected_attempt)
    if (not terminal or run.get("event") != "push"
            or run.get("workflow_id") != workflow["id"] or run.get("head_branch") != repo["default_branch"]
            or (run.get("head_repository") or {}).get("full_name", "").lower() != repository.lower()
            or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))):
        raise ValueError("Only successful default-branch push runs from this repository may deploy.")
    if expected_commit is not None and run['head_sha'] != expected_commit:
        raise ValueError('The source CI commit differs from the selected completion event.')
    if expected_attempt is not None and (type(expected_attempt) is not int or expected_attempt < 1
                                         or run.get('run_attempt') != expected_attempt):
        raise ValueError('The source CI attempt changed; wait for its own successful completion.')
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
        active = state['active']
        raise ValueError('An earlier delivery needs completion or verified recovery: https://github.com/'
                         + repository + '/actions/runs/' + active['run_id'] + ' (attempt '
                         + str(active['attempt']) + '). Inspect its original receipt; for a proven historical '
                         'package-verification failure before engine entry, see recover_preapply.py and docs/guides/CD_MODES.md.')
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
    application = isinstance(proof, dict) and proof.get('schema') == 'traffic-application-acceptance/v1'
    if current_run or application:
        from scripts.cicd.pipeline import current_source
        if not current_source(repository, run['head_sha'], fetch):
            raise Superseded('A newer application/controller source superseded this release')
    return {"sha": run["head_sha"], "run_id": str(run_id), "artifact": "backend-release-" + run["head_sha"],
            'mode': mode, 'epoch': str(state['epoch']), 'environment': environment_name, 'ci_number': str(run['run_number']),
            'ci_attempt': str(run['run_attempt']), 'operation':operation,
            'application':str(application).lower(), 'eligible':'true'}


def same_run(run, run_id, commit, attempt):
    """Only this push may consume its own completed CI jobs before CD finishes."""
    return (os.environ.get('GITHUB_EVENT_NAME') == 'push' and str(run_id) == os.environ.get('GITHUB_RUN_ID')
            and commit == os.environ.get('GITHUB_SHA') and str(attempt) == os.environ.get('GITHUB_RUN_ATTEMPT')
            and run.get('head_sha') == commit and str(run.get('run_attempt')) == str(attempt)
            and run.get('status') == 'in_progress' and run.get('conclusion') is None)


def recheck_source(repository, choice, *, fetch=get, current_run=False):
    """A rerun started while coverage/policy were being checked invalidates this choice."""
    run = fetch(f'repos/{repository}/actions/runs/{choice["run_id"]}')
    terminal = run.get('status') == 'completed' and run.get('conclusion') == 'success'
    if current_run:
        terminal = same_run(run, choice['run_id'], choice['sha'], int(choice['ci_attempt']))
    if (str(run.get('id')) != choice['run_id'] or run.get('head_sha') != choice['sha']
            or type(run.get('run_attempt')) is not int or str(run['run_attempt']) != choice['ci_attempt']
            or not terminal
            or run.get('event') != 'push'
            or (run.get('head_repository') or {}).get('full_name', '').lower() != repository.lower()):
        raise ValueError('The selected CI is no longer the same successful completed attempt.')
    if current_run or choice.get('application') == 'true':
        from scripts.cicd.pipeline import current_source
        if not current_source(repository, choice['sha'], fetch):
            raise Superseded('A newer application/controller source superseded this release')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument('--expected-commit')
    parser.add_argument('--expected-attempt', type=int)
    parser.add_argument('--check-only', action='store_true', help='Recheck the pinned CI before claim without changing step outputs.')
    parser.add_argument('--current-run', action='store_true')
    args = parser.parse_args()
    commit = args.expected_commit or os.environ.get('CI_TRIGGER_SHA') or None
    attempt = args.expected_attempt
    if attempt is None and os.environ.get('CI_TRIGGER_ATTEMPT'):
        attempt = int(os.environ['CI_TRIGGER_ATTEMPT'])
    if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_run' and (commit is None or attempt is None):
        raise ValueError('The CI completion event must identify its source commit and attempt.')
    if args.check_only:
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', os.environ['GITHUB_REPOSITORY'])
                or not re.fullmatch('[1-9][0-9]*', args.run_id)
                or not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit)
                or type(attempt) is not int or attempt < 1):
            raise ValueError('A pre-claim check requires the exact inspected CI identity.')
        choice = {'sha': commit, 'run_id': args.run_id, 'ci_attempt': str(attempt)}
        if os.environ.get('APPLICATION_RELEASE'):
            choice['application']=os.environ['APPLICATION_RELEASE']
        try:
            if args.current_run:
                recheck_source(os.environ['GITHUB_REPOSITORY'], choice, current_run=True)
            else:
                recheck_source(os.environ['GITHUB_REPOSITORY'], choice)
        except Superseded:
            print(json.dumps({'eligible':False,'reason':'Superseded before claim'}))
            return
        print(json.dumps({'ci_rechecked': True, 'eligible':True, **choice}))
        return
    options = {'current_run': True} if args.current_run else {}
    try:
        result = validate(os.environ["GITHUB_REPOSITORY"], args.run_id, operation=os.environ.get('DELIVERY_OPERATION','release'),
                          expected_commit=commit, expected_attempt=attempt, **options)
        recheck_source(os.environ['GITHUB_REPOSITORY'], result, **options)
    except Superseded:
        result={'eligible':'false','reason':'Superseded before deployment'}
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        for key, value in result.items():
            stream.write(key + "=" + value + "\n")
    print(json.dumps({"ci_provenance_verified": True, "deployment_policy_verified": True, 'ci_rechecked': True, **result}))
    if os.environ.get('GITHUB_STEP_SUMMARY') and result.get('eligible') != 'false':
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
            stream.write('## Deployment selection\n\n- Mode: **' + result['mode'] + '**\n- Environment: `' +
                         result['environment'] + '`\n- Commit: `' + result['sha'] + '`\n- Source CI: https://github.com/' +
                         os.environ['GITHUB_REPOSITORY'] + '/actions/runs/' + result['run_id'] + '\n')


if __name__ == "__main__":
    main()
