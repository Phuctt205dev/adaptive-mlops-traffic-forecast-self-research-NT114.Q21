"""Classify accumulated changes from the last verified production commit.

An absent/unusable baseline or unfamiliar path always requests the deep suite.
This selects test coverage only; it never changes CD policy or deploys anything.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from scripts.cicd.cd_state import GitHub,Store

SHA=re.compile(r'[0-9a-f]{40}')
SCHEMA='traffic-ci-scope/v1'
# Current delivery and image checks cover the managed topology on every release.
# Replay of the retired topology is required only for direct legacy changes,
# explicit/weekly validation, or an unknown impact/history. Shared controller,
# role/dependency and workflow edits still receive all current safety cases.
LEGACY_PREFIXES=('scripts/cicd/migrations/','deployment/rke2/migrations/',
            'tests/fixtures/legacy_region_cleanup/')
LEGACY_FILES={'deployment/rke2/baseline.json','deployment/rke2/role-baseline.json',
              'deployment/rke2/runtime/traffic_region_training_worker.py',
              # Changes to the proof contract/harness must exercise both
              # selected profiles before the new validator is released.
              'scripts/cicd/ci_scope.py','scripts/cicd/ci_coverage.py',
              'tests/integration/cluster_delivery.py','tests/integration/runtime_fixture.py'}
ORDINARY=('backend/','src/','tests/','scripts/cloud_training/','scripts/cicd/',
          'deployment/','requirements/','alembic/','.github/','frontend/','docs/')


def classify(paths,*,baseline,commit,force=False):
    if not SHA.fullmatch(commit):raise ValueError('Invalid source commit')
    paths=sorted(set(paths))
    reason='ordinary_source_change'
    if force:reason='explicit_full_validation'
    elif not isinstance(baseline,str) or not SHA.fullmatch(baseline):reason='missing_verified_baseline'
    elif any(p.startswith(LEGACY_PREFIXES) or p in LEGACY_FILES for p in paths):reason='legacy_migration_changed'
    elif any(not p.startswith(ORDINARY) and p not in {'app.py','README.md'} for p in paths):reason='unknown_impact'
    return {'schema':SCHEMA,'commit':commit,'baseline':baseline if isinstance(baseline,str) and SHA.fullmatch(baseline) else None,
            'full':reason!='ordinary_source_change','reason':reason,'paths':paths}


def git_paths(root,baseline,commit):
    def git(*args):
        return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True).stdout
    git('merge-base','--is-ancestor',baseline,commit)
    # --no-renames exposes both removed and added paths. This includes every
    # commit since the verified baseline, not merely HEAD~1 or the latest push.
    raw=git('diff','--name-only','--no-renames','-z',baseline,commit,'--')
    return [p.decode('utf-8',errors='strict') for p in raw.split(b'\0') if p]


def select(root,commit,*,state,force=False,diff=git_paths):
    baseline=(state.get('last_success') or {}).get('commit')
    try:
        if not isinstance(baseline,str) or not SHA.fullmatch(baseline):raise ValueError('No verified baseline')
        paths=diff(root,baseline,commit)
    except (ValueError,UnicodeError,subprocess.SubprocessError,OSError):
        result=classify([],baseline=None,commit=commit,force=force)
        result['reason']='unavailable_verified_history'
        return result
    return classify(paths,baseline=baseline,commit=commit,force=force)


def validate(value,commit):
    if not isinstance(value,dict) or value.get('schema')!=SCHEMA or value.get('commit')!=commit:
        raise ValueError('CI scope belongs to another commit')
    if type(value.get('full')) is not bool:raise ValueError('CI full scope must be boolean')
    paths=value.get('paths')
    if (not isinstance(paths,list) or any(not isinstance(p,str) or not p or p.startswith('/') or '\\' in p
            or '..' in p.split('/') or len(p)>4096 for p in paths) or paths!=sorted(set(paths))):
        raise ValueError('Invalid CI change paths')
    if value.get('reason')=='unavailable_verified_history':
        if value.get('baseline') is not None or paths or value.get('full') is not True:
            raise ValueError('Unknown history must require full coverage')
    else:
        expected=classify(paths,baseline=value.get('baseline'),commit=commit,
                          force=value.get('reason')=='explicit_full_validation')
        if value!=expected:raise ValueError('CI scope classification differs')
    return value


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',default='ci-scope.json');args=p.parse_args()
    # A read failure must increase coverage; it must never select a fast path.
    try:state=Store(GitHub(os.environ['GITHUB_REPOSITORY'])).load()[0]
    except Exception:state={}
    value=select(Path.cwd(),os.environ['GITHUB_SHA'],state=state,
                 force=os.environ['GITHUB_EVENT_NAME'] in {'schedule','workflow_dispatch','pull_request'})
    validate(value,os.environ['GITHUB_SHA'])
    Path(args.out).write_text(json.dumps(value,indent=2),encoding='utf-8')
    with Path(os.environ['GITHUB_OUTPUT']).open('a',encoding='utf-8') as f:f.write('full='+str(value['full']).lower()+'\n')
    print(json.dumps(value))


if __name__=='__main__':main()
