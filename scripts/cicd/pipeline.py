"""Application delivery scope and same-run acceptance. No cluster mutations.

Legacy CI proofs remain readable by ci_coverage. New proofs describe optional
jobs explicitly, so a skipped safety check cannot become an implicit success.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.cicd import ci_scope
from scripts.cicd.cd_state import GitHub, Store, emit

LEGACY_SCOPE = 'traffic-application-scope/v1'
V2_SCOPE = 'traffic-application-scope/v2'
SCOPE = 'traffic-application-scope/v3'
SCOPE_SCHEMAS = {LEGACY_SCOPE, V2_SCOPE, SCOPE}
ACCEPTANCE = 'traffic-application-acceptance/v1'
PR_SCOPE = 'traffic-pull-request-scope/v1'
RUNTIME_PREFIXES = ('scripts/cicd/', 'deployment/', 'requirements/roles/', 'alembic/',
                    '.github/workflows/', 'tests/integration/')
RUNTIME_FILES = {
    'backend/app/services/artifact_cache.py',
    'backend/app/services/restore_cache.py', 'backend/app/services/artifact_sync_control.py',
    'backend/app/services/region_cleanup_uploader.py', 'backend/app/services/lifecycle.py',
    'backend/app/services/serving_runtime.py', 'backend/app/services/worker_runtime.py',
    'backend/app/services/region_cleanup_runtime.py', 'src/cloud_training/airflow_control.py',
}
# Ordinary edits to existing roles/DAGs use image checks and the real CD
# preflight/Verify/rollback. Replaying a disposable cluster is reserved for
# controller/storage/protocol changes. New deployment paths stay conservative.
DAILY_DEPLOYMENT_FILES = {
    'deployment/rke2/roles/api.json', 'deployment/rke2/roles/tree.json',
    'deployment/rke2/roles/neural.json',
    'deployment/rke2/runtime/traffic_region_training_cloud.py',
}
DEEP_PREFIXES = ('scripts/cicd/', '.github/workflows/', 'tests/integration/', 'alembic/')
# These browser checks exercise synthetic API responses and are not packaged
# in application roles. Other scripts/shap tools include real rollout helpers
# and must not inherit this exemption.
UI_CHECK_FILES = {
    'scripts/shap/check_feature_importance_ui.py',
    'scripts/cloud_training/check_training_monitor_ui.py',
}


def backend_path(path):
    return (path in {'app.py', 'docs/index.html'} or
            path.startswith(('backend/', 'src/', 'scripts/', 'deployment/', 'requirements/roles/', 'alembic/')))


def pr_scope(paths, *, commit, base):
    """PR checks depend on their merge diff, not production history or secrets."""
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Invalid PR commit')
    known = paths is not None and isinstance(base, str) and re.fullmatch('[0-9a-f]{40}', base)
    paths = sorted(set(paths or []))
    if any(not isinstance(p, str) or not p or p.startswith('/') or '\\' in p or '..' in p.split('/') for p in paths):
        raise ValueError('Invalid PR paths')
    tests = frontend = not known
    for path in paths:
        if path in {'README.md', 'LICENSE', 'LICENSE.md', 'CONTRIBUTING.md', '.github/pull_request_template.md'} or (
                path.startswith('docs/') and path.endswith('.md')):
            continue
        if path.startswith('frontend/'):
            frontend = True
        elif (backend_path(path) or path.startswith(('tests/', 'requirements/')) or
              path in {'Dockerfile', 'requirements.txt', 'alembic.ini'}):
            tests = True
        else:
            # Workflow edits and unfamiliar inputs must not silently skip checks.
            tests = frontend = True
    return {'schema': PR_SCOPE, 'commit': commit, 'base': base if known else None,
            'paths': paths if known else [], 'tests': bool(tests), 'frontend': bool(frontend),
            'backend': bool(tests), 'current': False, 'full': False, 'forced': False,
            'reason': 'pull_request_diff' if known else 'unavailable_pull_request_diff'}


def validate_pr_scope(value, commit, base):
    if not isinstance(value, dict) or value.get('schema') != PR_SCOPE or value.get('commit') != commit:
        raise ValueError('PR coverage identity differs')
    if any(type(value.get(k)) is not bool for k in ('tests', 'frontend', 'backend', 'current', 'full', 'forced')):
        raise ValueError('PR coverage flags must be boolean')
    if value.get('reason') == 'unavailable_pull_request_diff':
        expected = pr_scope(None, commit=commit, base=None)
    else:
        expected = pr_scope(value.get('paths'), commit=commit, base=base)
    if value != expected:
        raise ValueError('PR coverage was modified or downgraded')
    return value


def required_pr_jobs(scope):
    return {'scope', 'acceptance'} | ({'tests'} if scope['tests'] else set()) | ({'frontend-build'} if scope['frontend'] else set())


def require_pr_checks(scope, results):
    required = required_pr_jobs(scope)
    for name in ('scope', 'tests', 'frontend-build'):
        result = results.get(name, {}).get('result')
        if result not in ({'success'} if name in required else {'success', 'skipped'}):
            raise ValueError('PR check did not pass: ' + name)


def classify_v1(paths, *, baseline, commit, force=False, frontend=False):
    """Frozen reader for accepted releases created before daily validation."""
    legacy = ci_scope.classify(paths, baseline=baseline, commit=commit, force=force)
    contract_changed = 'scripts/cicd/pipeline.py' in paths
    full = legacy['full'] or contract_changed
    current = full or any(p.startswith(RUNTIME_PREFIXES) or p in RUNTIME_FILES for p in paths)
    backend = full or current or any(backend_path(p) for p in paths)
    tests = backend or any(p.startswith('tests/') or p in {'requirements/ci.txt', 'requirements/backend.txt'} for p in paths)
    return {'schema': LEGACY_SCOPE, 'commit': commit, 'baseline': legacy['baseline'], 'paths': sorted(set(paths)),
            'backend': backend, 'tests': tests, 'frontend': bool(frontend), 'current': current,
            'full': full, 'forced': force,
            'reason': 'application_contract_changed' if contract_changed else legacy['reason']}


def classify_v2(paths, *, baseline, commit, force=False, frontend=False):
    """Frozen v2 reader: controller changes selected disposable clusters."""
    paths = sorted(set(paths))
    release_paths = [p for p in paths if p not in UI_CHECK_FILES]
    value = classify_v1(release_paths, baseline=baseline, commit=commit, force=force, frontend=frontend)
    current = value['full'] or any(
        p.startswith(DEEP_PREFIXES) or p in RUNTIME_FILES or
        (p.startswith('deployment/') and p not in DAILY_DEPLOYMENT_FILES) for p in release_paths)
    value.update(schema=V2_SCOPE, current=current, paths=paths)
    # A new or unfamiliar deployment input still builds a backend release,
    # even if it isn't under one of the application source directories.
    value['backend'] = current or any(backend_path(p) for p in release_paths)
    value['tests'] = value['backend'] or any(
        p.startswith('tests/') or p in UI_CHECK_FILES or
        p in {'requirements/ci.txt', 'requirements/backend.txt'} for p in paths)
    return value


def classify(paths, *, baseline, commit, force=False, frontend=False):
    """Every release is fast; only requested/weekly full checks create kind."""
    value = classify_v2(paths, baseline=baseline, commit=commit, force=force, frontend=frontend)
    value.update(schema=SCOPE, current=bool(force), full=bool(force))
    # Unknown inputs/history still run all fast checks. They never silently
    # skip regression, images or frontend, and cannot start a cluster on push.
    if value['reason'] in {'unknown_impact', 'missing_verified_baseline'}:
        value.update(backend=True, tests=True, frontend=True)
    return value


def validate_scope(value, commit):
    if not isinstance(value, dict) or value.get('schema') not in SCOPE_SCHEMAS or value.get('commit') != commit:
        raise ValueError('Application scope identity differs')
    paths = value.get('paths')
    if (not isinstance(paths, list) or paths != sorted(set(paths)) or
            any(not isinstance(p, str) or not p or p.startswith('/') or '\\' in p or '..' in p.split('/') for p in paths)):
        raise ValueError('Invalid application change paths')
    for key in ('backend', 'tests', 'frontend', 'current', 'full', 'forced'):
        if type(value.get(key)) is not bool:
            raise ValueError('Application scope flags must be boolean')
    classifier = {LEGACY_SCOPE:classify_v1, V2_SCOPE:classify_v2, SCOPE:classify}[value['schema']]
    expected = classifier(paths, baseline=value.get('baseline'), commit=commit,
                          force=value['forced'], frontend=value['frontend'])
    if value != expected:
        raise ValueError('Application coverage was downgraded or modified')
    return value


def required_results(scope):
    jobs = {'scope'}
    if scope['tests']:
        jobs.add('tests')
    if scope['backend']:
        jobs.update(('image', 'package'))
    if scope['frontend']:
        jobs.add('frontend-build')
    if scope['current']:
        jobs.add('current-runtime')
    if scope['full']:
        jobs.add('legacy-migration')
    return jobs


def required_jobs(scope):
    jobs = required_results(scope) | {'acceptance'}
    if 'image' in jobs:
        jobs.remove('image')
        jobs.update('image (' + role + ')' for role in ('api', 'tree', 'neural', 'sync'))
    return jobs


def aggregate(scope, proofs, results, *, commit, run_id, attempt, identity=None):
    from scripts.cicd.ci_coverage import validate_suite
    validate_scope(scope, commit)
    for name in required_results(scope):
        if results.get(name, {}).get('result') != 'success':
            raise ValueError('Required application job failed or skipped: ' + name)
    expected = ({'current'} if scope['current'] else set()) | ({'migration'} if scope['full'] else set())
    if set(proofs) != expected:
        raise ValueError('Application suite coverage differs from scope')
    if scope['backend']:
        if (not isinstance(identity, dict) or identity.get('commit') != commit or identity.get('schema') != 'traffic-delivery/v2'
                or not re.fullmatch('[0-9a-f]{64}', identity.get('release_id', ''))):
            raise ValueError('Missing immutable backend delivery')
        images=identity.get('images',{})
        if set(images) != {'api','tree','neural','sync'}:
            raise ValueError('Incomplete application image bindings')
        for role,binding in images.items():
            if (binding.get('role') != role or not re.fullmatch('[0-9a-f]{40}',binding.get('commit',''))
                    or not re.fullmatch('[0-9a-f]{64}',binding.get('source_id',''))
                    or not re.fullmatch(r'ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}',binding.get('image',''))):
                raise ValueError('Invalid application image binding')
        if identity.get('image') != images['api']['image']:
            raise ValueError('Application API image binding differs')
        for mode, proof in proofs.items():
            validate_suite(proof, mode, commit, run_id, attempt, identity)
    elif identity is not None:
        raise ValueError('Frontend-only acceptance cannot carry a backend release')
    return {'schema': ACCEPTANCE, 'commit': commit, 'ci_run_id': str(run_id), 'ci_attempt': str(attempt),
            'scope': scope, 'suites': proofs, 'identity': identity, 'deployable': True}


def check_acceptance(proof, run):
    if (proof.get('schema') != ACCEPTANCE or proof.get('commit') != run['head_sha'] or
            proof.get('ci_run_id') != str(run['id']) or proof.get('ci_attempt') != str(run['run_attempt']) or
            proof.get('deployable') is not True):
        raise ValueError('Application acceptance belongs to another commit/run/attempt')
    scope = validate_scope(proof['scope'], run['head_sha'])
    if scope['schema'] == SCOPE:
        if run.get('event') == 'push' and scope['forced']:
            raise ValueError('Release pushes cannot claim full validation')
        if run.get('event') == 'schedule' and not scope['forced']:
            raise ValueError('Scheduled validation must remain full')
    aggregate(scope, proof['suites'], {j: {'result': 'success'} for j in required_results(scope)},
              commit=run['head_sha'], run_id=run['id'], attempt=run['run_attempt'], identity=proof['identity'])
    return scope


def current_source(repository, commit, fetch):
    """Read the full trees: completion order never establishes source freshness."""
    repo = fetch('repos/' + repository)
    latest = fetch('repos/' + repository + '/git/ref/heads/' + repo['default_branch'])['object']['sha']
    if latest == commit:
        return True
    def inputs(sha):
        commit_tree=fetch('repos/'+repository+'/git/commits/'+sha)['tree']['sha']
        tree = fetch('repos/' + repository + '/git/trees/' + commit_tree + '?recursive=1')
        if tree.get('truncated') is not False:
            raise ValueError('Cannot establish current source from an incomplete tree')
        return {e['path']: e['sha'] for e in tree['tree'] if e.get('type') == 'blob' and
                (backend_path(e['path']) or e['path'].startswith('.github/workflows/'))}
    return inputs(commit) == inputs(latest)


def full_validation(env):
    if env['GITHUB_EVENT_NAME'] == 'schedule':
        return True
    if env['GITHUB_EVENT_NAME'] != 'workflow_dispatch':
        return False
    mode = env.get('VALIDATION_MODE', 'daily')
    if mode not in {'daily', 'full'}:
        raise ValueError('Unknown validation mode')
    return mode == 'full'


def scope_main():
    from scripts.cicd.frontend_release import PublicationStore, frontend_config
    from scripts.cicd.release import canonical, digest
    env = os.environ
    if env['GITHUB_EVENT_NAME'] == 'pull_request':
        base = env.get('PR_BASE_SHA', '')
        try:
            if not re.fullmatch('[0-9a-f]{40}', base):
                raise ValueError('PR base is unavailable')
            paths = ci_scope.git_paths(Path.cwd(), base, env['GITHUB_SHA'])
        except (ValueError, UnicodeError, subprocess.SubprocessError, OSError):
            paths = None
        value = pr_scope(paths, commit=env['GITHUB_SHA'], base=base)
        Path('ci-scope.json').write_text(json.dumps(value, indent=2), encoding='utf-8')
        if env.get('GITHUB_STEP_SUMMARY'):
            with Path(env['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
                stream.write('## PR checks\n\nBackend regression: '+str(value['tests'])+
                             '\n\nFrontend build: '+str(value['frontend'])+
                             '\n\nSource: '+value['reason']+'. Production deployment is disabled for PRs.\n')
        emit({k: value[k] for k in ('backend', 'tests', 'frontend', 'current', 'full')})
        return
    client = GitHub(env['GITHUB_REPOSITORY'])
    try:
        state = Store(client).load()[0]
        baseline = (state.get('last_success') or {}).get('commit')
        paths = ci_scope.git_paths(Path.cwd(), baseline, env['GITHUB_SHA']) if baseline else []
    except Exception:
        baseline, paths = None, []
    # A missing/diverged baseline selects all fast checks, not a kind cluster.
    frontend = True
    # Explicit/weekly full checks also validate an unchanged frontend.
    # Daily manual checks deduplicate just like ordinary pushes.
    force = full_validation(env)
    if not force:
        try:
            published, _ = PublicationStore(client).load()
            tree = subprocess.run(['git', 'rev-parse', env['GITHUB_SHA'] + ':frontend'],
                                  check=True, capture_output=True, text=True).stdout.strip()
            identifier = digest(canonical({'source': tree, **frontend_config(client.repository)}))
            frontend = not published or published['deployment_id'] != identifier
        except Exception:
            frontend = True
    value = classify(paths, baseline=baseline, commit=env['GITHUB_SHA'], frontend=frontend,
                     force=force)
    # PRs need regression but do not build/publish production images or proofs.
    validate_scope(value,env['GITHUB_SHA'])
    Path('ci-scope.json').write_text(json.dumps(value, indent=2), encoding='utf-8')
    if env.get('GITHUB_STEP_SUMMARY'):
        with Path(env['GITHUB_STEP_SUMMARY']).open('a',encoding='utf-8') as stream:
            stream.write('## Change scope\n\nBaseline: `'+str(value['baseline'])+'`\n\n')
            stream.write('| Stage | Required |\n| --- | --- |\n')
            for key in ('tests','backend','frontend','current','full'):
                stream.write('| '+key+' | '+str(value[key])+' |\n')
            stream.write('\nUnknown impact selects all fast checks. Full Kubernetes validation is manual or scheduled only. '
                         'Scope includes all changes since the verified backend.\n')
            stream.write('\nValidation: ' + ('full' if value['full'] else 'controller' if value['current'] else 'daily') +
                         '. Daily releases retain image checks and production preflight/Verify/rollback.\n')
    emit({k: value[k] for k in ('backend', 'tests', 'frontend', 'current', 'full')})


def accept_main():
    env = os.environ
    root = Path('coverage')
    raw_scope = json.loads((root / 'ci-scope.json').read_text())
    results = json.loads(env['CI_JOB_RESULTS'])
    if env['GITHUB_EVENT_NAME'] == 'pull_request':
        scope = validate_pr_scope(raw_scope, env['GITHUB_SHA'], env.get('PR_BASE_SHA', ''))
        require_pr_checks(scope, results)
        emit({'pull_request_checked': True, 'deployable': False})
        return
    scope = validate_scope(raw_scope, env['GITHUB_SHA'])
    if scope['schema'] == SCOPE and scope['forced'] != full_validation(env):
        raise ValueError('Validation selection differs from this workflow event/input')
    modes = (['current'] if scope['current'] else []) + (['migration'] if scope['full'] else [])
    identity = json.loads((root / 'release/delivery.json').read_text()) if scope['backend'] else None
    proofs = {m: json.loads((root / m / 'kubernetes-delivery-proof.json').read_text()) for m in modes}
    proof = aggregate(scope, proofs, results, commit=env['GITHUB_SHA'], run_id=env['GITHUB_RUN_ID'],
                      attempt=env['GITHUB_RUN_ATTEMPT'], identity=identity)
    (root / 'ci-acceptance.json').write_text(json.dumps(proof, indent=2), encoding='utf-8')
    emit({'accepted': True, 'backend': scope['backend'], 'frontend': scope['frontend'],
          'current_runtime_checked': scope['current'], 'legacy_checked': scope['full']})


def frontend_config_main():
    from scripts.cicd.frontend_release import frontend_config
    config = ({'api_url': 'https://ci.invalid/api/v1', 'base_path': '/ci/'}
              if os.environ['GITHUB_EVENT_NAME'] == 'pull_request' else frontend_config(os.environ['GITHUB_REPOSITORY']))
    with Path(os.environ['GITHUB_ENV']).open('a', encoding='utf-8') as stream:
        stream.write('VITE_API_BASE_URL=' + config['api_url'] + '\nVITE_BASE_PATH=' + config['base_path'] + '\n')
    emit(config)


def frontend_build_identity():
    env=os.environ
    tree=subprocess.run(['git','rev-parse',env['GITHUB_SHA']+':frontend'],
                        check=True,capture_output=True,text=True).stdout.strip()
    return {'commit':env['GITHUB_SHA'],'tree':tree,
            'config':{'api_url':env['VITE_API_BASE_URL'],'base_path':env['VITE_BASE_PATH']},
            'run_id':env['GITHUB_RUN_ID'],'attempt':env['GITHUB_RUN_ATTEMPT']}


def frontend_manifest_main():
    from scripts.cicd.frontend_release import build_manifest
    build_manifest('frontend',**frontend_build_identity())


def frontend_verify_main(directory='frontend-checked'):
    from scripts.cicd.frontend_release import verify_build
    verify_build(directory,**frontend_build_identity())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('scope', 'accept', 'frontend-config', 'frontend-manifest', 'frontend-verify'))
    action = parser.parse_args().action
    {'scope': scope_main, 'accept': accept_main, 'frontend-config': frontend_config_main,
     'frontend-manifest':frontend_manifest_main,'frontend-verify':frontend_verify_main}[action]()


if __name__ == '__main__':
    main()
