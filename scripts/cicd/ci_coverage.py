"""Fail closed on missing, skipped or mismatched required CI coverage."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import sys
import zipfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler,Request,build_opener

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from scripts.cicd.ci_scope import validate as validate_scope

SCHEMA='traffic-ci-acceptance/v1'
SUITE_SCHEMA='traffic-isolated-delivery-proof/v3'
CASES={'current':{'managed_update_with_long_request','worker_only_update_preserves_api',
                  'active_training_deferred_without_mutation','preview_smoke_rollback',
                  'post_promote_flush_failure_and_server_default_rollback','post_update_noop'},
       'migration':{'migration_required','legacy_preflight_failure_before_mutation','actual_migration'}}
COMMON_JOBS={'scope','tests','image (api)','image (tree)','image (neural)','image (sync)','package','current-runtime','acceptance'}


def validate_suite(proof,mode,commit,run_id,attempt,identity=None):
    if (proof.get('schema')!=SUITE_SCHEMA or proof.get('suite')!=mode
            or proof.get('source_commit')!=commit or str(proof.get('ci_run_id'))!=str(run_id)
            or str(proof.get('ci_attempt'))!=str(attempt) or proof.get('kubernetes_context')!='kind-cd-e2e'):
        raise ValueError('Isolated proof identity differs')
    for k,v in {'production_accessed':False,'business_http_is_fixture':True,'real_baseline_and_role_images':True,
                'real_rf_inference':True,'real_database_and_object_storage':True,'provider_called':False,
                'long_request_completed':True,'queue_preserved':True}.items():
        if proof.get(k) is not v:raise ValueError('Isolated safety coverage is incomplete: '+k)
    release=proof.get('release_id');images=proof.get('images')
    if (not isinstance(release,str) or not re.fullmatch('[0-9a-f]{64}',release) or not isinstance(images,dict)
            or set(images)!={'api','tree','neural','sync'}):raise ValueError('Missing checked release identity')
    for role,binding in images.items():
        if (binding.get('role')!=role or not re.fullmatch('[0-9a-f]{64}',binding.get('source_id',''))
                or not re.fullmatch('[0-9a-f]{40}',binding.get('commit',''))
                or not re.fullmatch(r'ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}',binding.get('image',''))):
            raise ValueError('Invalid checked image identity')
    if identity is not None and (release!=identity['release_id'] or images!=identity['images']):
        raise ValueError('Suite checked another immutable release')
    cases=proof.get('cases',[])
    if not isinstance(cases,list) or len(cases)!=len(CASES[mode]) or {c.get('case') for c in cases}!=CASES[mode]:
        raise ValueError('Required isolated cases are missing or duplicated')
    for c in cases:
        name=c['case']
        if c.get('queued_database_rows')!=1 or c.get('guard_held') is not False or c.get('recovery_required') is not False:
            raise ValueError('Queue or recovery coverage failed: '+name)
        if name=='post_update_noop' and c.get('up_to_date') is not True:
            raise ValueError('No-op was not proven')
        if name=='active_training_deferred_without_mutation' and c.get('deferred') is not True:
            raise ValueError('Active training guard was not proven')
        if name in {'preview_smoke_rollback','post_promote_flush_failure_and_server_default_rollback'} and c.get('rolled_back') is not True:
            raise ValueError('Rollback was not proven')
        if name in {'actual_migration','managed_update_with_long_request','worker_only_update_preserves_api'} and c.get('completed') is not True:
            raise ValueError('Update was not verified')
        if name=='managed_update_with_long_request':
            flush=c.get('flush_proofs')
            if not flush or any(f.get('complete') is not True or f.get('stable') is not True for f in flush):
                raise ValueError('Cache flush acknowledgement missing')
        if name=='managed_update_with_long_request' and c.get('manifest_only_update') is not True:
            raise ValueError('Manifest-only coverage was not merged into the API update')
    return proof


def required_results(scope):
    return {'scope','tests','image','package','current-runtime'}|({'legacy-migration'} if scope['full'] else set())


def aggregate(scope,proofs,results,*,commit,run_id,attempt,identity):
    validate_scope(scope,commit)
    if any(results.get(k,{}).get('result')!='success' for k in required_results(scope)):
        raise ValueError('A required CI job did not succeed')
    expected={'current','migration'} if scope['full'] else {'current'}
    if set(proofs)!=expected:raise ValueError('Isolated suite coverage differs from scope')
    for mode,proof in proofs.items():validate_suite(proof,mode,commit,run_id,attempt,identity)
    return {'schema':SCHEMA,'commit':commit,'ci_run_id':str(run_id),'ci_attempt':str(attempt),
            'scope':scope,'release_id':identity['release_id'],'images':identity['images'],'suites':proofs,'deployable':True}


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        if urlsplit(newurl).scheme!='https':raise ValueError('Refusing an insecure artifact redirect')
        redirected=super().redirect_request(request,fp,code,msg,headers,newurl)
        if urlsplit(request.full_url).netloc!=urlsplit(newurl).netloc:
            redirected.remove_header('Authorization')
        return redirected


def read_artifact(repository,run_id,name,fetch):
    values=[];page=1
    while True:
        data=fetch(f'repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100&page={page}')
        values.extend(a for a in data['artifacts'] if a.get('name')==name and not a.get('expired'))
        if len(data['artifacts'])<100:break
        page+=1
        if page>20:raise ValueError('Artifact listing exceeds its budget')
    if len(values)!=1:raise ValueError('Exactly one acceptance artifact is required')
    artifact=values[0]
    if not isinstance(artifact.get('id'),int):raise ValueError('Invalid acceptance artifact')
    request=Request(f'https://api.github.com/repos/{repository}/actions/artifacts/{artifact["id"]}/zip',
                    headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json'})
    with build_opener(SafeRedirect()).open(request,timeout=60) as response:raw=response.read(4*1024*1024+1)
    if len(raw)>4*1024*1024:raise ValueError('Acceptance artifact exceeds its budget')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if archive.namelist()!=['ci-acceptance.json'] or archive.getinfo('ci-acceptance.json').file_size>4*1024*1024:
            raise ValueError('Invalid acceptance artifact envelope')
        return json.loads(archive.read('ci-acceptance.json'))


def check_run(repository,run,fetch,*,artifact=read_artifact,require_backend=True):
    run_id=str(run['id']);attempt=run['run_attempt'];commit=run['head_sha']
    proof=artifact(repository,run_id,'ci-acceptance-'+commit,fetch)
    from scripts.cicd import pipeline
    if proof.get('schema') == pipeline.ACCEPTANCE:
        scope = pipeline.check_acceptance(proof, run)
        if require_backend and not scope['backend']:
            raise ValueError('This application run contains no backend release')
        jobs = []
        for page in range(1, 21):
            data = fetch(f'repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}')
            jobs.extend(data['jobs'])
            if len(data['jobs']) < 100:
                break
        else:
            raise ValueError('CI job listing exceeds its budget')
        for name in pipeline.required_jobs(scope):
            matches = [j for j in jobs if j.get('name') == name]
            if len(matches) != 1 or matches[0].get('status') != 'completed' or matches[0].get('conclusion') != 'success':
                raise ValueError('Required application job missing, skipped or unsuccessful: ' + name)
        return proof
    if (proof.get('schema')!=SCHEMA or proof.get('commit')!=commit or proof.get('ci_run_id')!=run_id
            or proof.get('ci_attempt')!=str(attempt) or proof.get('deployable') is not True):
        raise ValueError('CI acceptance does not match the successful run attempt')
    scope=validate_scope(proof.get('scope'),commit)
    identity={'release_id':proof['release_id'],'images':proof['images']}
    aggregate(scope,proof.get('suites',{}),{k:{'result':'success'} for k in required_results(scope)},
              commit=commit,run_id=run_id,attempt=attempt,identity=identity)
    jobs=[];page=1
    while True:
        data=fetch(f'repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}')
        jobs.extend(data['jobs'])
        if len(data['jobs'])<100:break
        page+=1
        if page>20:raise ValueError('CI job listing exceeds its budget')
    expected=COMMON_JOBS|({'legacy-migration'} if scope['full'] else set())
    for name in expected:
        matches=[j for j in jobs if j.get('name')==name]
        if len(matches)!=1 or matches[0].get('status')!='completed' or matches[0].get('conclusion')!='success':
            raise ValueError('Required CI job missing, skipped or unsuccessful: '+name)
    return proof


def main():
    p=argparse.ArgumentParser();p.add_argument('--directory',default='coverage');args=p.parse_args()
    root=Path(args.directory);env=os.environ;commit=env['GITHUB_SHA']
    scope=json.loads((root/'ci-scope.json').read_text());results=json.loads(env['CI_JOB_RESULTS'])
    # PRs run the regression suite, but cannot create a deployable acceptance.
    if env['GITHUB_EVENT_NAME']=='pull_request':
        if any(results.get(k,{}).get('result')!='success' for k in ('scope','tests')):
            raise ValueError('Pull request regression checks failed')
        print(json.dumps({'pull_request_checked':True,'deployable':False}));return
    identity=json.loads((root/'release/delivery.json').read_text())
    modes=['current','migration'] if scope['full'] else ['current']
    proofs={mode:json.loads((root/mode/'kubernetes-delivery-proof.json').read_text()) for mode in modes}
    value=aggregate(scope,proofs,results,commit=commit,run_id=env['GITHUB_RUN_ID'],attempt=env['GITHUB_RUN_ATTEMPT'],identity=identity)
    (root/'ci-acceptance.json').write_text(json.dumps(value,indent=2),encoding='utf-8')
    with Path(env['GITHUB_STEP_SUMMARY']).open('a',encoding='utf-8') as f:
        f.write('## CI coverage accepted\n\nCommit: `'+commit+'`\n\nScope: **'+('full' if scope['full'] else 'regular')+
                '** ('+scope['reason']+')\n\n')
        for mode in modes:
            f.write('- '+mode+': '+str(len(proofs[mode]['cases']))+' cases; '+str(proofs[mode]['duration_seconds'])+' seconds\n')
        f.write('\nScheduled and manual validation runs do not deploy production.\n')
    print(json.dumps({'ci_coverage_verified':True,'full':scope['full'],'commit':commit}))


if __name__=='__main__':main()
