"""Publish frontend source only against a verified, compatible backend release."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from scripts.cicd.cd_state import GitHub,Store,emit,ApiError,BRANCH
from scripts.cicd.release import canonical,digest


def backend_path(path):
    return (path=='app.py' or path.startswith(('backend/','src/','tests/','scripts/','deployment/','requirements/','alembic/'))
            or path.startswith(('.github/workflows/backend-','.github/workflows/cd-')) and path.endswith('.yml'))


def snapshot(client,sha):
    commit=client.request('GET','git/commits/'+sha)
    entries={e['path']:e for e in client.request('GET','git/trees/'+commit['tree']['sha'])['tree']}
    workflow={}
    if '.github' in entries:
        github={e['path']:e for e in client.request('GET','git/trees/'+entries['.github']['sha'])['tree']}
        if 'workflows' in github:
            workflow={e['path']:e['sha'] for e in client.request('GET','git/trees/'+github['workflows']['sha'])['tree']
                      if e['path'].startswith(('backend-','cd-')) and e['path'].endswith('.yml')}
    return {'frontend':entries['frontend']['sha'],
            'backend':{p:e['sha'] for p,e in entries.items() if backend_path(p+'/') or p=='app.py'},
            'workflows':workflow}


def frontend_config(repository):
    url=os.environ.get('FRONTEND_API_URL','');parsed=urlsplit(url)
    base=os.environ.get('FRONTEND_BASE_PATH','') or '/'+repository.split('/')[1]+'/'
    if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path.rstrip('/')!='/api/v1' or '\n' in url or '\r' in url):
        raise ValueError('Configure the reviewed HTTPS frontend API URL')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]*',base):raise ValueError('Invalid frontend base path')
    return {'api_url':url,'base_path':base}


class PublicationStore:
    """A separate CAS file; never changes CD mode, ownership or failure latch."""
    path='contents/frontend-publication.json'
    def __init__(self,client):self.client=client
    def load(self):
        try:
            value=self.client.request('GET',self.path+'?ref='+BRANCH)
        except ApiError as e:
            if e.status==404:return None,None
            raise
        raw=base64.b64decode(''.join(value['content'].split()),validate=True)
        if len(raw)>8192:raise ValueError('Invalid frontend publication record')
        data=json.loads(raw)
        if (data.get('schema')!='traffic-frontend-publication/v1' or not re.fullmatch('[0-9a-f]{64}',data.get('deployment_id',''))
                or not re.fullmatch('[0-9a-f]{40}',data.get('commit',''))):
            raise ValueError('Invalid frontend publication record')
        return data,value['sha']
    def save(self,data):
        _,sha=self.load()
        body={'branch':BRANCH,'message':'Record verified frontend publication '+data['commit'],
              'content':base64.b64encode((json.dumps(data)+'\n').encode()).decode()}
        if sha:body['sha']=sha
        self.client.request('PUT',self.path,body)


def select(client,sha,backend_run='',*,trees=snapshot):
    if not re.fullmatch('[0-9a-f]{40}',sha):raise ValueError('Invalid frontend source identity')
    state=Store(client).load()[0];success=state.get('last_success')
    if not success:return {'publish':False,'sha':sha,'reason':'No verified backend release'}
    default=client.request('GET','')['default_branch']
    latest=client.request('GET','git/ref/heads/'+default)['object']['sha']
    selected=trees(client,sha);current=selected if sha==latest else trees(client,latest)
    verified=selected if success['commit']==sha else trees(client,success['commit'])
    if selected['frontend']!=current['frontend']:
        return {'publish':False,'sha':sha,'reason':'A newer frontend source is waiting'}
    if backend_run and str(success['run_id'])!=str(backend_run):
        return {'publish':False,'sha':sha,'reason':'Backend verification belongs to another release'}
    if selected['backend']!=verified['backend'] or selected['workflows']!=verified['workflows']:
        return {'publish':False,'sha':sha,'reason':'Waiting for this backend source to pass Apply and Verify'}
    config=frontend_config(client.repository)
    identifier=digest(canonical({'source':selected['frontend'],**config}))
    published,_=PublicationStore(client).load()
    if published and published['deployment_id']==identifier:
        return {'publish':False,'sha':sha,'reason':'This frontend content and configuration are already published'}
    return {'publish':True,'sha':sha,'deployment_id':identifier,'backend_commit':success['commit'],
            **config,'reason':'Backend source is verified or unchanged'}


def dispatch(client,sha,run_id):
    state=Store(client).load()[0];success=state.get('last_success')
    if (state.get('active') or not success or success['commit']!=sha or str(success['run_id'])!=str(run_id)):
        raise ValueError('Frontend publication requires this completed verified backend release')
    default=client.request('GET','')['default_branch']
    latest=client.request('GET','git/ref/heads/'+default)['object']['sha']
    selection=select(client,latest,str(run_id))
    if not selection['publish']:return {'frontend_dispatched':False,'reason':selection['reason']}
    client.request('POST','actions/workflows/deploy-frontend-pages.yml/dispatches',
                   {'ref':default,'inputs':{'source_sha':latest,'backend_run_id':str(run_id)}})
    return {'frontend_dispatched':True,'sha':latest,'backend_run_id':str(run_id)}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['select','dispatch','record']);p.add_argument('--commit',required=True)
    p.add_argument('--backend-run',default='')
    p.add_argument('--deployment-id')
    args=p.parse_args();client=GitHub(os.environ['GITHUB_REPOSITORY'])
    if args.action=='record':
        if not re.fullmatch('[0-9a-f]{40}',args.commit) or not re.fullmatch('[0-9a-f]{64}',args.deployment_id or ''):
            raise ValueError('Invalid frontend publication identity')
        PublicationStore(client).save({'schema':'traffic-frontend-publication/v1','commit':args.commit,
                                      'deployment_id':args.deployment_id,'run_id':os.environ['GITHUB_RUN_ID']})
        result={'frontend_publication_recorded':True}
    else:
        result=select(client,args.commit,args.backend_run) if args.action=='select' else dispatch(client,args.commit,args.backend_run)
    emit(result)


if __name__=='__main__':main()
