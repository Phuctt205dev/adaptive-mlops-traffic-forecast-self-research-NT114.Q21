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
    from scripts.cicd.pipeline import backend_path as application_input
    return application_input(path)


def snapshot(client,sha):
    commit=client.request('GET','git/commits/'+sha)
    data=client.request('GET','git/trees/'+commit['tree']['sha']+'?recursive=1')
    if data.get('truncated') is not False:raise ValueError('Incomplete source tree')
    entries={e['path']:e for e in data['tree']}
    return {'frontend':entries['frontend']['sha'],
            'backend':{p:e['sha'] for p,e in entries.items() if e.get('type')=='blob' and backend_path(p)},
            'workflows':{}}


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
    if state.get('active'):return {'publish':False,'sha':sha,'reason':'A backend transaction is still active'}
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


def dispatch(client,sha,run_id,artifact_run_id=''):
    state=Store(client).load()[0];success=state.get('last_success')
    if (state.get('active') or not success or success['commit']!=sha or str(success['run_id'])!=str(run_id)):
        raise ValueError('Frontend publication requires this completed verified backend release')
    default=client.request('GET','')['default_branch']
    latest=client.request('GET','git/ref/heads/'+default)['object']['sha']
    source=sha if artifact_run_id else latest
    if artifact_run_id:
        artifacts=client.request('GET','actions/runs/'+str(artifact_run_id)+'/artifacts?name=frontend-build-'+source+'&per_page=100')
        matches=[a for a in artifacts.get('artifacts',[]) if a.get('name')=='frontend-build-'+source and not a.get('expired')]
        if len(matches)!=1:
            return {'frontend_dispatched':False,'reason':'No unique checked frontend artifact in this source run'}
    selection=select(client,source,str(run_id))
    if not selection['publish']:return {'frontend_dispatched':False,'reason':selection['reason']}
    inputs={'source_sha':source,'backend_run_id':str(run_id)}
    if artifact_run_id:inputs['artifact_run_id']=str(artifact_run_id)
    client.request('POST','actions/workflows/deploy-frontend-pages.yml/dispatches',{'ref':default,'inputs':inputs})
    return {'frontend_dispatched':True,'sha':source,'backend_run_id':str(run_id)}


def build_manifest(directory, *, commit, tree, config, run_id, attempt):
    root=Path(directory)
    # npm creates valid symlinks under node_modules/.bin on Linux. Only the
    # deployable dist and its manifest belong to this artifact boundary.
    if (root.is_symlink() or (root/'dist').is_symlink() or (root/'frontend-build.json').is_symlink()
            or any(p.is_symlink() for p in (root/'dist').rglob('*'))):
        raise ValueError('Frontend build cannot contain symlinks')
    files={p.relative_to(root).as_posix():digest(p.read_bytes()) for p in sorted((root/'dist').rglob('*')) if p.is_file()}
    if not files:
        raise ValueError('Frontend build must contain regular output files')
    value={'schema':'traffic-frontend-build/v1','commit':commit,'tree':tree,'config':config,
           'ci_run_id':str(run_id),'ci_attempt':str(attempt),'files':files}
    (root/'frontend-build.json').write_text(json.dumps(value,indent=2),encoding='utf-8')
    return value


def verify_build(directory, *, commit, tree, config, run_id, attempt):
    root=Path(directory)
    if root.is_symlink() or (root/'frontend-build.json').is_symlink():
        raise ValueError('Frontend artifact cannot contain symlinks')
    value=json.loads((root/'frontend-build.json').read_text(encoding='utf-8'))
    if (value.get('schema')!='traffic-frontend-build/v1' or value.get('commit')!=commit or value.get('tree')!=tree
            or value.get('config')!=config or value.get('ci_run_id')!=str(run_id) or value.get('ci_attempt')!=str(attempt)):
        raise ValueError('Frontend artifact source/config/run/attempt differs')
    if (root/'dist').is_symlink() or any(p.is_symlink() for p in (root/'dist').rglob('*')):
        raise ValueError('Frontend artifact cannot contain symlinks')
    files={p.relative_to(root).as_posix():digest(p.read_bytes()) for p in (root/'dist').rglob('*') if p.is_file()}
    if not files or files!=value.get('files'):raise ValueError('Frontend artifact files differ')
    return value


def select_artifact(client, sha, run_id, directory, backend_run=''):
    from scripts.cicd.ci_coverage import check_run
    run=client.request('GET','actions/runs/'+str(run_id))
    workflow=client.request('GET','actions/workflows/backend-ci.yml')
    default=client.request('GET','')['default_branch']
    if (str(run.get('id'))!=str(run_id) or run.get('head_sha')!=sha or run.get('event')!='push'
            or run.get('head_branch')!=default or run.get('workflow_id')!=workflow['id']
            or (run.get('head_repository') or {}).get('full_name','').lower()!=client.repository.lower()):
        raise ValueError('Frontend artifact must come from a default-branch application push')
    # A Pages-only failure can be retried from the same immutable CI build.
    proof=check_run(client.repository,run,
        lambda p:client.request('GET',p.removeprefix('repos/'+client.repository+'/')),
        require_backend=False)
    if not proof['scope'].get('frontend'):raise ValueError('This run did not check a frontend build')
    result=select(client,sha,backend_run)
    config=frontend_config(client.repository)
    tree=snapshot(client,sha)['frontend']
    verify_build(directory,commit=sha,tree=tree,config=config,run_id=run_id,attempt=run['run_attempt'])
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['select','select-artifact','dispatch','record']);p.add_argument('--commit',required=True)
    p.add_argument('--backend-run',default='')
    p.add_argument('--deployment-id')
    p.add_argument('--artifact-run-id',default='')
    p.add_argument('--directory',default='frontend-checked')
    args=p.parse_args();client=GitHub(os.environ['GITHUB_REPOSITORY'])
    if args.action=='record':
        if not re.fullmatch('[0-9a-f]{40}',args.commit) or not re.fullmatch('[0-9a-f]{64}',args.deployment_id or ''):
            raise ValueError('Invalid frontend publication identity')
        PublicationStore(client).save({'schema':'traffic-frontend-publication/v1','commit':args.commit,
                                      'deployment_id':args.deployment_id,'run_id':os.environ['GITHUB_RUN_ID']})
        result={'frontend_publication_recorded':True}
    else:
        if args.action=='select-artifact':
            result=select_artifact(client,args.commit,args.artifact_run_id,args.directory,args.backend_run)
        else:
            result=select(client,args.commit,args.backend_run) if args.action=='select' else dispatch(client,args.commit,args.backend_run,args.artifact_run_id)
    emit(result)


if __name__=='__main__':main()
