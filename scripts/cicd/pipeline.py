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

SCOPE = 'traffic-application-scope/v1'
ACCEPTANCE = 'traffic-application-acceptance/v1'
RUNTIME_PREFIXES = ('scripts/cicd/', 'deployment/', 'requirements/roles/', 'alembic/',
                    '.github/workflows/', 'tests/integration/')
RUNTIME_FILES = {
    'backend/app/services/restore_cache.py', 'backend/app/services/artifact_sync_control.py',
    'backend/app/services/region_cleanup_uploader.py', 'backend/app/services/lifecycle.py',
    'backend/app/services/serving_runtime.py', 'backend/app/services/worker_runtime.py',
    'backend/app/services/region_cleanup_runtime.py', 'src/cloud_training/airflow_control.py',
}


def backend_path(path):
    return (path in {'app.py', 'docs/index.html'} or
            path.startswith(('backend/', 'src/', 'scripts/', 'deployment/', 'requirements/roles/', 'alembic/')))


def classify(paths, *, baseline, commit, force=False, frontend=False):
    legacy = ci_scope.classify(paths, baseline=baseline, commit=commit, force=force)
    contract_changed = 'scripts/cicd/pipeline.py' in paths
    full = legacy['full'] or contract_changed
    current = full or any(p.startswith(RUNTIME_PREFIXES) or p in RUNTIME_FILES for p in paths)
    backend = full or current or any(backend_path(p) for p in paths)
    tests = backend or any(p.startswith('tests/') or p in {'requirements/ci.txt', 'requirements/backend.txt'} for p in paths)
    return {'schema': SCOPE, 'commit': commit, 'baseline': legacy['baseline'], 'paths': sorted(set(paths)),
            'backend': backend, 'tests': tests, 'frontend': bool(frontend), 'current': current,
            'full': full, 'forced': force,
            'reason': 'application_contract_changed' if contract_changed else legacy['reason']}


def validate_scope(value, commit):
    if not isinstance(value, dict) or value.get('schema') != SCOPE or value.get('commit') != commit:
        raise ValueError('Application scope identity differs')
    paths = value.get('paths')
    if (not isinstance(paths, list) or paths != sorted(set(paths)) or
            any(not isinstance(p, str) or not p or p.startswith('/') or '\\' in p or '..' in p.split('/') for p in paths)):
        raise ValueError('Invalid application change paths')
    for key in ('backend', 'tests', 'frontend', 'current', 'full', 'forced'):
        if type(value.get(key)) is not bool:
            raise ValueError('Application scope flags must be boolean')
    expected = classify(paths, baseline=value.get('baseline'), commit=commit,
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


def scope_main():
    from scripts.cicd.frontend_release import PublicationStore, frontend_config
    from scripts.cicd.release import canonical, digest
    env = os.environ
    client = GitHub(env['GITHUB_REPOSITORY'])
    try:
        state = Store(client).load()[0]
        baseline = (state.get('last_success') or {}).get('commit')
        paths = ci_scope.git_paths(Path.cwd(), baseline, env['GITHUB_SHA']) if baseline else []
    except Exception:
        baseline, paths = None, []
    # A missing/diverged baseline selects every safety check, never a fast path.
    frontend = True
    # Explicit/weekly full checks also validate an unchanged frontend. Only
    # ordinary pushes deduplicate against the existing Pages publication.
    if env['GITHUB_EVENT_NAME'] not in {'pull_request','workflow_dispatch','schedule'}:
        try:
            published, _ = PublicationStore(client).load()
            tree = subprocess.run(['git', 'rev-parse', env['GITHUB_SHA'] + ':frontend'],
                                  check=True, capture_output=True, text=True).stdout.strip()
            identifier = digest(canonical({'source': tree, **frontend_config(client.repository)}))
            frontend = not published or published['deployment_id'] != identifier
        except Exception:
            frontend = True
    value = classify(paths, baseline=baseline, commit=env['GITHUB_SHA'], frontend=frontend,
                     force=env['GITHUB_EVENT_NAME'] in {'schedule', 'workflow_dispatch'})
    # PRs need regression but do not build/publish production images or proofs.
    validate_scope(value,env['GITHUB_SHA'])
    Path('ci-scope.json').write_text(json.dumps(value, indent=2), encoding='utf-8')
    if env.get('GITHUB_STEP_SUMMARY'):
        with Path(env['GITHUB_STEP_SUMMARY']).open('a',encoding='utf-8') as stream:
            stream.write('## Change scope\n\nBaseline: `'+str(value['baseline'])+'`\n\n')
            stream.write('| Stage | Required |\n| --- | --- |\n')
            for key in ('tests','backend','frontend','current','full'):
                stream.write('| '+key+' | '+str(value[key])+' |\n')
            stream.write('\nUnknown impact selects full validation. Scope includes all changes since the verified backend.\n')
    emit({k: value[k] for k in ('backend', 'tests', 'frontend', 'current', 'full')})


def accept_main():
    env = os.environ
    root = Path('coverage')
    scope = validate_scope(json.loads((root / 'ci-scope.json').read_text()), env['GITHUB_SHA'])
    results = json.loads(env['CI_JOB_RESULTS'])
    if env['GITHUB_EVENT_NAME'] == 'pull_request':
        required = {'scope', 'tests', 'frontend-build'}
        if any(results.get(j, {}).get('result') != 'success' for j in required):
            raise ValueError('PR regression or frontend build did not pass')
        emit({'pull_request_checked': True, 'deployable': False})
        return
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
