"""Retry an explicitly deferred idle-only deployment; never override CD policy."""
import base64
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from scripts.cicd.cd_state import ApiError, BRANCH, GitHub, Store

FILE='cd-pending.json'
MAX_RETRIES=12
RETRY_WINDOW_SECONDS=6*60*60


def load(api):
    try:
        doc=api.request('GET','contents/'+FILE+'?ref='+BRANCH)
    except ApiError as error:
        if error.status==404:
            return None,None
        raise
    raw=base64.b64decode(''.join(doc['content'].split()),validate=True)
    if len(raw)>4096 or doc.get('type')!='file' or doc.get('encoding')!='base64':
        raise ValueError('Invalid deferred delivery envelope')
    value=json.loads(raw)
    required={'commit','ci_run_id','ci_number'}
    if value is not None and (not required.issubset(value) or set(value)-required-{'retry_count','expires_at'} or
        not re.fullmatch('[0-9a-f]{40}',value.get('commit','')) or not str(value.get('ci_run_id','')).isdigit()
        or type(value.get('ci_number')) is not int or value['ci_number']<1):
        raise ValueError('Invalid deferred delivery identity')
    if value is not None and (type(value.get('retry_count',0)) is not int or value.get('retry_count',0)<0
            or type(value.get('expires_at',0)) not in {int,float} or value.get('expires_at',0)<0):
        raise ValueError('Invalid deferred retry budget')
    return value,doc['sha']


def save(api, value, *, completed_number=None):
    for retry in range(5):
        old,sha=load(api)
        if value is not None and old and old['ci_number']>value['ci_number']:
            return
        if completed_number is not None and old and old['ci_number']>completed_number:
            return
        if value is not None:
            value=dict(value)
            same=old and all(old[k]==value[k] for k in ('commit','ci_run_id','ci_number'))
            value.setdefault('retry_count',old.get('retry_count',0) if same else 0)
            value.setdefault('expires_at',old.get('expires_at',time.time()+RETRY_WINDOW_SECONDS) if same else time.time()+RETRY_WINDOW_SECONDS)
        if old==value:
            return
        body={'branch':BRANCH,'message':'Record deferred deployment queue','content':base64.b64encode(json.dumps(value).encode()).decode()}
        if sha:
            body['sha']=sha
        try:
            api.request('PUT','contents/'+FILE,body)
            return
        except ApiError as error:
            if error.status not in {409,422} or retry==4:
                raise


def retry(api):
    state,_=Store(api).load()
    pending,_=load(api)
    if not pending or state['mode']!='auto' or state['blocked'] or state['active']:
        return {'retry_dispatched':False,'reason':'No eligible deferred Auto delivery'}
    if state.get('last_success') and state['last_success']['ci_number']>=pending['ci_number']:
        save(api,None,completed_number=state['last_success']['ci_number'])
        return {'retry_dispatched':False,'reason':'A newer release already completed'}
    if pending.get('retry_count',0)>=MAX_RETRIES or pending.get('expires_at',time.time()+RETRY_WINDOW_SECONDS)<=time.time():
        return {'retry_dispatched':False,'reason':'Deferred retry budget exhausted; inspect before requesting a manual release'}
    enabled=api.request('GET','actions/variables/RKE2_CD_ENABLED')
    if enabled.get('value')!='true':
        return {'retry_dispatched':False,'reason':'CD disabled'}
    for workflow in ('backend-deploy.yml','backend-ci.yml'):
        for status in ('queued','in_progress','waiting','requested'):
            runs=api.request('GET','actions/workflows/'+workflow+'/runs?status='+status+'&branch=master&per_page=1')
            if runs.get('total_count',0):
                return {'retry_dispatched':False,'reason':'Application or deployment workflow already active'}
    from scripts.cicd.github_gate import validate,Superseded
    try:
        choice=validate(api.repository,pending['ci_run_id'],fetch=lambda p:api.request('GET',p.removeprefix('repos/'+api.repository+'/').removeprefix('repos/'+api.repository)))
    except Superseded:
        save(api,None,completed_number=pending['ci_number'])
        return {'retry_dispatched':False,'reason':'A newer application source superseded this deferred release'}
    if choice['sha']!=pending['commit'] or int(choice['ci_number'])!=pending['ci_number']:
        raise ValueError('Deferred CI provenance changed')
    save(api,{**pending,'retry_count':pending.get('retry_count',0)+1})
    api.request('POST','actions/workflows/backend-deploy.yml/dispatches',{'ref':'master','inputs':{'ci_run_id':pending['ci_run_id']}})
    return {'retry_dispatched':True,'ci_run_id':pending['ci_run_id']}


if __name__=='__main__':
    try:
        print(json.dumps(retry(GitHub(os.environ['GITHUB_REPOSITORY']))))
    except Exception as error:
        print(json.dumps({'deferred_retry_failed':True,'error_type':type(error).__name__}))
        raise SystemExit(1)
