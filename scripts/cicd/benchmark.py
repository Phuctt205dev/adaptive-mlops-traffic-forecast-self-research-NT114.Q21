"""Read-only delivery measurements. Missing evidence remains unknown, never zero.

This is observation, not an acceptance gate or a source of deployment authority.
The native summary observes a prefix of its still-running workflow; it never
claims to measure total workflow duration, incidents or reviewer waiting time.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
from statistics import median
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.cicd.cd_state import GitHub
from scripts.cicd import pipeline, ci_scope, ci_coverage

SCHEMA = 'traffic-delivery-metrics/v1'
CI_JOBS = {'scope', 'tests', 'frontend-build', 'package', 'current-runtime',
           'legacy-migration', 'acceptance'} | {'image ('+r+')' for r in ('api','tree','neural','sync')}
SCENARIOS = ('auto','frontend-only','API-only','worker-DAG','runtime-manifest','no-op','mixed','unknown')


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def seconds(start, end):
    a, b = timestamp(start), timestamp(end)
    return round(b-a, 3) if a is not None and b is not None and b >= a else None


def scenario(scope):
    if scope is None: return 'unknown'
    paths = scope['paths']
    if scope['frontend']: return 'mixed' if scope['backend'] else 'frontend-only'
    if not scope['backend']: return 'no-op'
    if paths and all(p.startswith(('src/', 'deployment/airflow/')) for p in paths): return 'worker-DAG'
    if scope['current']: return 'runtime-manifest'
    if paths and all(p.startswith(('backend/', 'app.py', 'docs/index.html')) for p in paths): return 'API-only'
    return 'mixed'


def scope_profile(scope, commit):
    """Read the previous pipeline too, preserving its always-current coverage."""
    if scope is None: return None, None, set()
    if scope.get('schema') == pipeline.PR_SCOPE:
        pipeline.validate_pr_scope(scope, commit, scope.get('base'))
        return scope, {k:scope[k] for k in ('tests','backend','frontend','current','full')}, pipeline.required_pr_jobs(scope)
    if scope.get('schema') in pipeline.SCOPE_SCHEMAS:
        pipeline.validate_scope(scope,commit)
        return scope, {k:scope[k] for k in ('tests','backend','frontend','current','full')}, pipeline.required_jobs(scope)
    ci_scope.validate(scope,commit)
    inferred = pipeline.classify(scope['paths'],baseline=scope['baseline'],commit=commit,
                                 force=scope['full'],frontend=False)
    coverage = {'tests':True,'backend':True,'frontend':False,'current':True,'full':scope['full']}
    return inferred, coverage, ci_coverage.COMMON_JOBS | ({'legacy-migration'} if scope['full'] else set())


def proof_scope(proof, run):
    if proof.get('schema') == pipeline.ACCEPTANCE: return pipeline.check_acceptance(proof,run)
    if (proof.get('schema') != ci_coverage.SCHEMA or proof.get('commit') != run['head_sha']
            or proof.get('ci_run_id') != str(run['id']) or proof.get('ci_attempt') != str(run['run_attempt'])
            or proof.get('deployable') is not True):
        raise ValueError('Benchmark proof belongs to another run/attempt')
    scope=ci_scope.validate(proof['scope'],run['head_sha'])
    ci_coverage.aggregate(scope,proof['suites'],{j:{'result':'success'} for j in ci_coverage.required_results(scope)},
                          commit=run['head_sha'],run_id=run['id'],attempt=run['run_attempt'],
                          identity={'release_id':proof['release_id'],'images':proof['images']})
    return scope


def jobs_for(client, run):
    """Exact attempt pagination; refuse partial or mixed-attempt observations."""
    jobs = []
    for page in range(1, 21):
        batch = client.request('GET', f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100&page={page}")
        jobs.extend(batch['jobs'])
        if len(jobs) >= batch['total_count']:
            if len(jobs) != batch['total_count']: raise ValueError('Inconsistent job pagination')
            break
    else: raise ValueError('Job listing exceeds observation budget')
    if len({j['id'] for j in jobs}) != len(jobs): raise ValueError('Duplicate job observations')
    for job in jobs:
        # The REST job schema may omit run_attempt. The attempt-specific
        # endpoint above supplies that binding; reject contradictions if sent.
        if (str(job.get('run_id')) != str(run['id']) or job.get('run_attempt',run['run_attempt']) != run['run_attempt']
                or job.get('head_sha') != run['head_sha']):
            raise ValueError('Job belongs to another source/run/attempt')
    return jobs


def pr_tested_commit(client, run, scope):
    """PRs test a synthetic merge; REST head_sha still names the source head."""
    if run.get('event') != 'pull_request' or scope.get('schema') != pipeline.PR_SCOPE:
        raise ValueError('Not a PR measurement')
    tested = scope.get('commit')
    if not isinstance(tested, str) or not re.fullmatch('[0-9a-f]{40}', tested):
        raise ValueError('Invalid tested PR commit')
    if tested == run['head_sha']:
        return tested
    merge = client.request('GET', 'git/commits/'+tested)
    parents = {p['sha'] for p in merge.get('parents', [])}
    matches = [p for p in run.get('pull_requests', [])
               if p.get('head', {}).get('sha') == run['head_sha']
               and (scope.get('base') is None or p.get('base', {}).get('sha') == scope['base'])
               and parents == {run['head_sha'], p.get('base', {}).get('sha')}]
    if merge.get('sha') != tested or len(merge.get('parents', [])) != 2 or len(matches) != 1:
        raise ValueError('PR merge parents differ from the observed run')
    return tested


def measure(run, jobs, *, scope=None, receipt=None, commit_at=None, context=None, tested_commit=None):
    context = context or {}
    if scope and scope.get('schema') == pipeline.PR_SCOPE and (run.get('event') != 'pull_request' or receipt is not None):
        raise ValueError('PR measurements cannot authorize or describe production delivery')
    if tested_commit is not None and run.get('event') != 'pull_request':
        raise ValueError('Only PR measurements have a separate tested merge commit')
    inferred, coverage, required = scope_profile(scope,tested_commit or run['head_sha'])
    # Exclude orchestration summary and reusable deployment jobs from CI time.
    ci = [j for j in jobs if j['name'] in CI_JOBS]
    acceptance = [j for j in ci if j['name'] == 'acceptance']
    ci_ok = bool(required) and all(len(matches := [j for j in ci if j['name'] == n]) == 1
                                 and matches[0].get('conclusion') == 'success'
                                 and matches[0].get('status') == 'completed' for n in required)
    end = acceptance[0].get('completed_at') if len(acceptance) == 1 else None
    starts = [j.get('started_at') for j in ci if timestamp(j.get('started_at')) is not None]
    first = min(starts, key=timestamp) if starts else None
    durations = [seconds(j.get('started_at'),j.get('completed_at')) for j in ci
                 if j.get('status') == 'completed' and j.get('conclusion') != 'skipped']
    images = {}
    for job in ci:
        if job['name'].startswith('image ('):
            successful = {s['name'] for s in job.get('steps',[]) if s.get('conclusion') == 'success'}
            reused = 'Pull previously checked immutable image' in successful
            built = 'Publish only after image checks pass' in successful
            images[job['name'][7:-1]] = 'reuse' if reused and not built else 'build' if built and not reused else 'unknown'
    image_mode = ('none' if not images else next(iter(set(images.values())))
                  if len(set(images.values())) == 1 else 'mixed')
    outcome, deployed_at, recovery = 'not_observed', None, None
    phases, components = [], []
    if receipt:
        if receipt.get('schema') != 'traffic-role-delivery/v2' or receipt.get('commit') != run['head_sha']:
            raise ValueError('Receipt source/schema differs')
        safe = receipt.get('guard_held') is False and receipt.get('recovery_required') is False
        if receipt.get('deferred') is True:
            outcome = ('deferred' if safe and receipt.get('started') is False and not receipt.get('patched')
                       and receipt.get('preflight_only') is True and receipt.get('completed') is False else 'unknown')
        elif receipt.get('rolled_back') is True and safe:
            outcome = 'rolled_back'
            recovery = seconds(receipt.get('failed_at'),receipt.get('recovered_at'))
        elif receipt.get('recovery_required') is True: outcome = 'recovery_required'
        elif receipt.get('completed') is True and safe:
            if receipt.get('up_to_date') is True:
                outcome = 'no_op' if receipt.get('started') is False and not receipt.get('patched') else 'unknown'
            elif receipt.get('started') is True and receipt.get('patched'):
                outcome, deployed_at = 'verified_deploy', receipt.get('updated_at')
            else: outcome = 'unknown'
        elif receipt.get('error_type'): outcome = 'failed_before_verification'
        else: outcome = 'unknown'
        components = [{'component':c['component'],'planned_action':c['action']} for c in receipt.get('components',[])
                      if c.get('component') in {'api','tree','neural','airflow'} and c.get('action') in {'deploy','keep'}]
        history = receipt.get('phase_history',[])[-32:]
        for a,b in zip(history,history[1:]):
            # Only emit known phase names, never arbitrary receipt strings.
            if a.get('phase') in {'plan','admission','stage_api','stage_workers','update_airflow',
                                  'promote','verify_promoted','retire','release_admission','rollback'}:
                phases.append({'phase':a['phase'],'seconds':seconds(a.get('at'),b.get('at'))})
    pages = [s for j in jobs if j['name'].split(' / ')[0] == 'frontend-publication'
             for s in j.get('steps',[]) if s.get('name') == 'Publish the verified artifact']
    published = len(pages) == 1 and pages[0].get('conclusion') == 'success'
    page_at = pages[0].get('completed_at') if published else None
    # Never pass arbitrary API context into exported artifacts (runner names etc).
    clean_context = {key:context.get(key,'unknown') for key in ('experiment','workload','cache_state','runner_profile')}
    for key,value in clean_context.items():
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}',value):
            raise ValueError('Invalid benchmark annotation: '+key)
    selected = context.get('scenario','auto')
    if selected not in SCENARIOS: raise ValueError('Invalid benchmark scenario')
    return {'schema':SCHEMA,'source':'github-metadata','run_id':str(run['id']),
            'attempt':run['run_attempt'],'commit':run['head_sha'],'event':run['event'],
            'workflow_complete':run.get('status') == 'completed','conclusion':run.get('conclusion'),
            'observation':'completed-workflow' if run.get('status') == 'completed' else 'prefix-before-summary-completion',
            'context':{**clean_context,'scenario':scenario(inferred) if selected == 'auto' else selected,
                       'image_mode':image_mode},
            'coverage':coverage,
            'ci_accepted':ci_ok,'timing_sample_eligible':ci_ok and run.get('conclusion') != 'cancelled',
            'timestamps':{key:value if timestamp(value) is not None else None for key,value in {
                'run_created_at':run.get('created_at'),'ci_started_at':first,
                'ci_accepted_at':end if ci_ok else None,'backend_verified_at':deployed_at,
                'pages_published_at':page_at}.items()},
            'seconds':{'ci_wall':seconds(run.get('created_at'),end) if run['run_attempt'] == 1 else None,
                       'ci_execution_span':seconds(first,end),
                       'initial_wait':seconds(run.get('created_at'),first) if run['run_attempt'] == 1 else None,
                       'ci_runner':round(sum(durations),3) if durations and None not in durations else None,
                       'backend_commit_to_verify':seconds(commit_at,deployed_at),
                       'pages_commit_to_publish':seconds(commit_at,page_at),
                       'failed_release_to_verified_rollback':recovery},
            'tested_commit':tested_commit or run['head_sha'],
            'images':images,'backend':outcome,'pages_published':published,'phases':phases,'component_plan':components,
            'jobs':[{'name':j['name'],'conclusion':j.get('conclusion'),
                     'seconds':seconds(j.get('started_at'),j.get('completed_at'))} for j in ci]}


def report(value):
    lines = ['## CI/CD measurements','',
             'Observation: '+value['observation']+'. CI accepted: '+str(value['ci_accepted'])+'.',
             'Backend: '+value['backend']+'. Pages published: '+str(value['pages_published'])+'.','',
             '| Measurement | Seconds |','| --- | ---: |']
    lines += ['| '+key+' | '+('unknown' if number is None else str(number))+' |' for key,number in value['seconds'].items()]
    if value['images']:
        lines += ['', '| Image role | Build decision |','| --- | --- |']
        lines += ['| '+role+' | '+action+' |' for role,action in sorted(value['images'].items())]
    if value['component_plan']:
        lines += ['', '| Component | Planned action (see backend outcome) |','| --- | --- |']
        lines += ['| '+item['component']+' | '+item['planned_action']+' |' for item in value['component_plan']]
    lines += ['', 'CI runner seconds sum parallel job occupancy; CI wall measures elapsed time.',
              'Initial wait includes scheduling/dependencies; reviewer/training waits are not separately measured.',
              'Recovery measures receipt failure to verified rollback, not confirmed production incident recovery.',
              'No-op/deferred are not new deployments. DORA incident/rework rates require incident records.',
              'Native summary excludes its own duration; total workflow duration is not measured here.','']
    lines += ['On reruns, CI wall/initial wait are unknown: created_at belongs to the original run.','']
    return '\n'.join(lines)


def compare(baseline, candidate):
    """Pilot medians, paired by explicit workload/conditions. Never silently pool."""
    groups = defaultdict(lambda: [[],[]])
    seen = set()
    counts = [Counter(),Counter()]
    for side,values in enumerate((baseline,candidate)):
        for value in values:
            if value.get('schema') != SCHEMA or value.get('source') != 'github-metadata':
                raise ValueError('Only measured GitHub records may enter a comparison')
            identity = (value['run_id'],value['attempt'])
            if identity in seen: raise ValueError('Duplicate run/attempt across benchmark inputs')
            seen.add(identity)
            counts[side][value['backend']] += 1
            if not value['timing_sample_eligible']:
                counts[side]['excluded_timing'] += 1
                continue
            key = tuple(value['context'][k] for k in ('experiment','workload','scenario','cache_state','runner_profile','image_mode'))+(value['event'],)
            groups[key][side].append(value)
    rows = []
    for key,sides in sorted(groups.items()):
        controlled = 'unknown' not in key and key[-1] == 'push'
        coverage = [sorted({json.dumps(v['coverage'],sort_keys=True) for v in values}) for values in sides]
        for metric in ('ci_wall','ci_execution_span','ci_runner','backend_commit_to_verify','pages_commit_to_publish'):
            stats = []
            for values in sides:
                numbers = [v['seconds'].get(metric) for v in values if v['seconds'].get(metric) is not None]
                stats.append({'n':len(numbers),'median':median(numbers) if numbers else None,
                              'min':min(numbers) if numbers else None,'max':max(numbers) if numbers else None})
            enough = controlled and all(s['n'] >= 5 for s in stats)
            before,after = stats
            delta = round((before['median']-after['median'])/before['median']*100,2) if enough and before['median'] else None
            rows.append({'conditions':list(key),'metric':metric,'baseline':before,'candidate':after,
                         'status':'pilot' if enough else 'insufficient_samples_or_conditions',
                         'reduction_percent':delta,'coverage_changed':coverage[0] != coverage[1]})
    return {'schema':'traffic-delivery-comparison/v1','outcomes':[dict(c) for c in counts],'rows':rows}


def read_json(path):
    if not path.is_file(): return None
    if path.is_symlink() or path.stat().st_size > 2*1024*1024: raise ValueError('Invalid metric input file')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action',required=True)
    collect = sub.add_parser('collect')
    collect.add_argument('--run-id',default=os.environ.get('GITHUB_RUN_ID'))
    collect.add_argument('--attempt',type=int,default=int(os.environ.get('GITHUB_RUN_ATTEMPT','1')))
    collect.add_argument('--inputs',type=Path,default=Path('metrics-input'))
    collect.add_argument('--output',type=Path,default=Path('delivery-metrics'))
    collect.add_argument('--scenario',choices=SCENARIOS,default='auto')
    for key in ('experiment','workload','cache-state','runner-profile'):
        collect.add_argument('--'+key,default=os.environ.get('BENCHMARK_'+key.upper().replace('-','_')) or 'unknown')
    diff = sub.add_parser('compare')
    diff.add_argument('--baseline',type=Path,nargs='+',required=True)
    diff.add_argument('--candidate',type=Path,nargs='+',required=True)
    diff.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if args.action == 'compare':
        result = compare([read_json(p) for p in args.baseline],[read_json(p) for p in args.candidate])
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        return
    if not str(args.run_id).isdigit() or args.attempt < 1: raise ValueError('Invalid observation identity')
    client = GitHub(os.environ['GITHUB_REPOSITORY'])
    run = client.request('GET',f'actions/runs/{args.run_id}/attempts/{args.attempt}')
    if str(run['id']) != str(args.run_id) or run['run_attempt'] != args.attempt:
        raise ValueError('Observation attempt changed')
    jobs = jobs_for(client,run)
    scope = read_json(args.inputs/'scope/ci-scope.json')
    proof = read_json(args.inputs/'acceptance/ci-acceptance.json')
    if proof is not None: scope = proof_scope(proof,run)
    receipt = read_json(args.inputs/'receipt'/f"delivery-{run['head_sha']}-{run['id']}-{run['run_attempt']}.json")
    if receipt is not None and proof is not None:
        identity = proof.get('identity') or proof
        if receipt.get('release_id') != identity.get('release_id'):
            raise ValueError('Measurement receipt differs from accepted release')
    commit_at = client.request('GET','git/commits/'+run['head_sha']).get('committer',{}).get('date')
    tested = pr_tested_commit(client, run, scope) if scope and scope.get('schema') == pipeline.PR_SCOPE else None
    result = measure(run,jobs,scope=scope,receipt=receipt,commit_at=commit_at,context=vars(args),tested_commit=tested)
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'delivery-metrics.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    rendered = report(result)
    (args.output/'report.md').write_text(rendered,encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf-8') as stream: stream.write(rendered)


if __name__ == '__main__': main()
