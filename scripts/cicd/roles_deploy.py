"""Role-scoped blue/green delivery. No training, migrations or user-data rollback.

The old API cohort stays live until the preview is warm and checked. Dispatch
is paused durably in Airflow; the lease never holds the serving advisory lock.
Resource inverse specifications stay beside the private kubeconfig, outside
checkout and outside uploaded receipts. Kubernetes changes use UID/version CAS.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.cicd.deploy import Cluster, container, ready_pods, now, spec_hash, AIRFLOW_FILES, DAG_CHECK, DAG_COMPATIBILITY_CHECK
from scripts.cicd.release import canonical, digest, DIGEST_IMAGE
from scripts.cicd.roles_release import verify, ROLES
from scripts.cicd.role_manifests import render,contract,CONTRACT,ANNOTATION,SLOTS
from scripts.cicd import diagnostics

SERVICES = ('traffic-api','traffic-api-training','api-peers','api-preview',
            'traffic-training-tree','traffic-training-worker')
STATUS = "import json;from pathlib import Path;p=Path('/tmp/role-guard-status.json');print(p.read_text() if p.exists() else json.dumps({'state':'starting'}))"
IDLE = '''import json,requests
from sqlalchemy import text
from backend.app.db.session import SessionLocal
from backend.app.core.config import get_settings
s=get_settings()
with SessionLocal() as db:
 heads=list(db.execute(text('SELECT version_num FROM alembic_version')).scalars())
 active=db.execute(text("SELECT count(*) FROM training_executions WHERE state IN ('running','finalizing','cancelling')")).scalar_one()
with requests.Session() as c:
 c.trust_env=False
 base=s.airflow_api_url+'/dags/'+s.training_dag_id
 r=c.get(base,auth=(s.airflow_admin_username,s.airflow_admin_password),timeout=10);r.raise_for_status()
 paused=r.json()['is_paused']
 r=c.get(base+'/dagRuns',params={'state':'running','limit':1},auth=(s.airflow_admin_username,s.airflow_admin_password),timeout=10);r.raise_for_status()
 running=r.json()['total_entries']
 drift=s.airflow_api_url+'/dags/'+s.drift_dag_id
 r=c.get(drift,auth=(s.airflow_admin_username,s.airflow_admin_password),timeout=10);r.raise_for_status()
 drift_paused=r.json()['is_paused']
 r=c.get(drift+'/dagRuns',params={'state':'running','limit':1},auth=(s.airflow_admin_username,s.airflow_admin_password),timeout=10);r.raise_for_status()
 running+=r.json()['total_entries']
print(json.dumps({'heads':heads,'active':active,'running':running,'paused':paused,'drift_paused':drift_paused}))
'''


def validate_delivery(release, value):
    if value.get('schema') != 'traffic-delivery/v2' or value.get('commit') != release['commit'] or value.get('release_id') != release['release_id']:
        raise ValueError('Role delivery identity differs')
    images = value.get('images', {})
    if set(images) != set(ROLES) or value.get('image') != images['api'].get('image'):
        raise ValueError('Role delivery is incomplete')
    for role, image in images.items():
        if (image.get('role') != role or image.get('source_id') != release['roles'][role]['source_id']
                or not DIGEST_IMAGE.fullmatch(image.get('image','')) or not image['image'].startswith('ghcr.io/')
                or not re.fullmatch('[0-9a-f]{40}', image.get('commit',''))):
            raise ValueError('Invalid immutable role image')
    return images


class RolesDelivery:
    def __init__(self, cluster, directory, binding, journal, *, commit, kubeconfig, migration=None):
        self.cluster, self.directory, self.path = cluster, Path(directory), Path(journal)
        self.release = verify(directory, expected_commit=commit)
        bound=json.loads(Path(binding).read_text(encoding='utf-8-sig'))
        self.images = validate_delivery(self.release, bound)
        self.contract=contract(directory)
        wanted={role:render(role,slots[0],self.images,directory=directory)['spec']['template']['metadata']['annotations'][ANNOTATION]
                for role,slots in SLOTS.items()}
        if bound.get('runtime_contract')!=CONTRACT or bound.get('deployments')!=wanted:
            raise ValueError('Role deployment configuration binding differs')
        self.state = {'schema':'traffic-role-delivery/v2', 'commit':commit, 'release_id':self.release['release_id'],
                      'image':self.images['api']['image'], 'images':self.images, 'patched':[],
                      'preflight_only':True, 'started':False, 'completed':False, 'rolled_back':False,
                      'guard_held':False,'recovery_required':False}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            if self.state['commit'] != commit or self.state['release_id'] != self.release['release_id']:
                raise ValueError('Receipt belongs to another release')
        private_root = Path(kubeconfig).resolve().parent / 'role-transactions'
        self.private_path = private_root / self.path.name
        self.inverses = json.loads(self.private_path.read_text()) if self.private_path.exists() else []
        self.migration=None
        selected=migration or self.state.get('migration')
        if selected:
            if selected!='role-runtime-v1':raise ValueError('Unsupported migration selection')
            from scripts.cicd.migrations.role_runtime_v1 import Migration
            self.migration=Migration(self)
            self.state['migration']=selected

    def save(self, **values):
        previous_phase=self.state.get('phase')
        self.state.update(values, updated_at=now())
        if self.state.get('phase') != previous_phase:
            history=self.state.get('phase_history',[])
            self.state['phase_history']=[*history[-31:],{'phase':self.state['phase'],'at':self.state['updated_at']}]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        p = self.path.with_suffix('.tmp')
        p.write_text(json.dumps(self.state, indent=2), encoding='utf-8')
        p.replace(self.path)
        if self.state.get('phase')!=previous_phase:
            print(json.dumps({'delivery_phase':self.state['phase'],'updated_at':self.state['updated_at']}),flush=True)

    def get(self, kind, name):
        return self.cluster.call('get',kind,name,'-o','json')

    def remote(self,deployment,source):
        return self.execute(self.cluster.pod(deployment),container(deployment)[1]['name'],source)

    def execute(self,pod,name,source,*,timeout=100):
        """Keep sanitized exception facts even when every application pod is Ready."""
        wrapped=("import json,traceback\ntry:\n exec("+repr(source)+")\n"
                 "except Exception as e:\n print(json.dumps({'execution_failed':True,'error_type':type(e).__name__,"
                 "'frames':[{'file':f.filename.replace(chr(92),'/').rsplit('/',1)[-1],'line':f.lineno} for f in traceback.extract_tb(e.__traceback__)[-8:]]}))")
        result=self.cluster.call('exec',pod,'-c',name,'--','python','-B','-c',wrapped,timeout=timeout)
        if result.get('execution_failed'):
            fact={'phase':self.state.get('phase','preflight'),'pod':diagnostics.identifier(pod),
                  'container':diagnostics.identifier(name),'error_type':diagnostics.identifier(result.get('error_type')),
                  'frames':[{'file':diagnostics.identifier(f.get('file')),'line':f.get('line')} for f in result.get('frames',[]) if type(f.get('line')) is int]}
            self.save(**{'recovery_execution_failure' if self.state.get('phase')=='rollback' else 'execution_failure':fact})
            raise RuntimeError('Runtime operation failed: '+fact['error_type'])
        return result

    def require_flush(self,proof,name):
        if (proof.get('schema')!='traffic-artifact-sync/v1' or proof.get('complete') is not True
                or proof.get('stable') is not True or type(proof.get('acknowledged_files')) is not int):
            fact={'phase':self.state.get('phase','retire'),'workload':name,
                  'error_type':diagnostics.identifier(proof.get('error_type','UnconfirmedFlush'))}
            self.save(**{'recovery_execution_failure' if self.state.get('phase')=='rollback' else 'execution_failure':fact})
            raise RuntimeError('Artifact flush was not fully acknowledged; retirement stopped')
        self.save(flush_proofs=[*self.state.get('flush_proofs',[]),{'workload':name,**{k:proof[k] for k in ('schema','complete','stable','acknowledged_files')}}])

    def change(self, kind, name, spec):
        """Write-ahead inverse + exact after hash, then version-checked replace."""
        current = self.get(kind,name)
        # Hash the server-defaulted spec, not merely the submitted JSON. For
        # example a ConfigMap volume gains defaultMode=420 on admission. The
        # same UID/version tests cover dry-run and mutation; concurrent edits
        # still fail rather than being accepted as part of this transaction.
        spec = self.patch(kind,current,[{'op':'replace','path':'/spec','value':spec}],dry=True)['spec']
        if current['spec'] == spec:
            return current
        plan = {'kind':kind,'name':name,'uid':current['metadata']['uid'],
                'before':copy.deepcopy(current['spec']), 'after':digest(canonical(spec))}
        self.inverses.append(plan)
        self.private_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.private_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.inverses),encoding='utf-8')
        temporary.replace(self.private_path)
        self.save(patched=[*self.state['patched'],name], recovery_required=True)
        return self.patch(kind,current,[{'op':'replace','path':'/spec','value':spec}])

    def patch(self, kind, current, operations, *, dry=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'patch.json'
            args = ['patch',kind,current['metadata']['name'],'--type=json','--patch-file='+str(path),'-o','json']
            if dry:
                args.append('--dry-run=server')
            observed=current
            for attempt in range(3):
                tests = [{'op':'test','path':'/metadata/uid','value':observed['metadata']['uid']},
                         {'op':'test','path':'/metadata/resourceVersion','value':observed['metadata']['resourceVersion']}]
                path.write_text(json.dumps(tests+operations),encoding='utf-8')
                try:
                    return self.cluster.call(*args)
                except RuntimeError:
                    latest=self.get(kind,current['metadata']['name'])
                    # Controller status writes may advance resourceVersion.
                    # Never adopt spec drift, replacements or an uncertain write.
                    if (attempt==2 or latest['metadata']['uid']!=current['metadata']['uid']
                            or latest['spec']!=current['spec']
                            or latest['metadata']['resourceVersion']==observed['metadata']['resourceVersion']):
                        raise
                    observed=latest

    def scale(self, name, count):
        value = self.get('deployment',name)
        spec = copy.deepcopy(value['spec']); spec['replicas'] = count
        return self.change('deployment',name,spec)

    def route(self, service, app):
        value = self.get('service',service)
        spec = copy.deepcopy(value['spec']); spec['selector'] = {'app':app}
        return self.change('service',service,spec)

    def api(self):
        app = self.get('service','traffic-api')['spec']['selector'].get('app')
        allowed=set(SLOTS['api'])
        if getattr(self,'migration',None):allowed.add(self.migration.plan['roles']['api'])
        if app not in allowed:
            if app=='traffic-api':raise ValueError('Runtime migration required; select the reviewed role-runtime-v1 migration')
            raise RuntimeError('Unknown active API cohort')
        return self.cluster.deployment(app)

    def pause(self, value=None, *, drift=False):
        expression = ('c.get(base,auth=auth,timeout=10)' if value is None else
                      'c.patch(base,json={"is_paused":'+repr(value)+'},auth=auth,timeout=10)')
        code = ("import requests,json;from backend.app.core.config import get_settings;s=get_settings();"
                "c=requests.Session();c.trust_env=False;base=s.airflow_api_url+'/dags/'+s."+('drift_dag_id' if drift else 'training_dag_id')+";"
                "auth=(s.airflow_admin_username,s.airflow_admin_password);r="+expression+
                ";r.raise_for_status();print(json.dumps({'paused':r.json()['is_paused']}))")
        actual = self.remote(self.api(),code)['paused']
        if value is not None and actual is not value:
            raise RuntimeError('DAG pause state was not confirmed')
        return actual

    def health(self, name, role, *, target=True):
        deployment = self.cluster.deployment(name)
        pods = ready_pods(deployment,self.cluster.call('get','pods','-o','json')['items'])
        desired = 2 if role == 'api' else 1
        if len(pods) != desired:
            raise RuntimeError('The ready role replica count differs: '+name)
        prefix = '/api/v1/health' if role == 'api' else '/health'
        proofs = []
        for pod in pods:
            main = container(deployment)[1]
            source = "import json;from urllib.request import urlopen;print(json.dumps(json.load(urlopen('http://127.0.0.1:8000"+prefix+"/release',timeout=10))))"
            actual = self.cluster.call('exec',pod['metadata']['name'],'-c',main['name'],'--','python','-B','-c',source)
            binding = self.images[role] if target else {'source_id':deployment['spec']['template']['metadata'].get('annotations',{}).get('traffic.mlops/source-id'),
                                                       'commit':actual.get('commit'),'image':main['image']}
            if (actual.get('source_id') != binding['source_id'] or actual.get('commit') != binding['commit']
                    or actual.get('image_role') != role or actual.get('role')!=('api' if role=='api' else 'tree' if role=='tree' else 'recurrent')
                    or main['image'] != binding['image']
                    or actual.get('runtime_contract')!=CONTRACT or not re.fullmatch('[0-9a-f]{40}',actual.get('commit',''))):
                raise RuntimeError('Running role differs from its immutable binding')
            if role == 'api':
                sync = [c for c in pod['spec'].get('initContainers',[]) if c['name']=='artifact-sync']
                statuses = pod['status'].get('initContainerStatuses',[])
                if len(sync)!=1 or (target and sync[0]['image']!=self.images['sync']['image']) or not any(s['name']=='artifact-sync' and s.get('ready') for s in statuses):
                    raise RuntimeError('The native artifact sidecar is not ready')
                check = "from deployment.rke2.runtime.role_runtime import verify;import json;d=verify();print(json.dumps({'source_id':d['source_id']}))"
                result = self.cluster.call('exec',pod['metadata']['name'],'-c','artifact-sync','--','python','-B','-c',check)
                if target and result.get('source_id')!=self.images['sync']['source_id']:
                    raise RuntimeError('Sidecar source identity differs')
                capabilities=self.execute(pod['metadata']['name'],'artifact-sync',
                    "from backend.app.services.artifact_sync_control import main;main(['capabilities'])")
                if capabilities.get('schema')!='traffic-artifact-sync/v1' or 'flush' not in capabilities.get('actions',[]):
                    raise RuntimeError('Sidecar flush contract is unavailable')
            proofs.append({'deployment':name,'pod':pod['metadata']['name'],'role':role,'source_id':actual['source_id'],'verified':True})
        return proofs

    def business(self, host='traffic-api'):
        script = (Path(__file__).with_name('role_probe.py')).read_text(encoding='utf-8')
        script = script.split("if __name__ == '__main__':")[0] + '\nprint(json.dumps(run_check('+repr('http://'+host+':8000/api/v1')+')))'
        # A quality report takes the prediction runtime lock without waiting.
        # The observer must not compete with our own full report and create
        # a false busy response. Full checks themselves continue observing HTTP.
        with self.probe_lock():
            result = self.cluster.remote(self.api(),script)
        self.require_probe(result,host)
        self.save(business_proofs=[*self.state.get('business_proofs',[]),{'target':host,**result}])
        return result

    def probe_lock(self):
        if not hasattr(self,'_probe_lock'):
            self._probe_lock=threading.Lock()
        return self._probe_lock

    def require_probe(self,result,target):
        if result.get('probe_failed') or result.get('errors') != 0:
            # Remote probes return allowlisted facts, never raw exception text,
            # URLs, JWTs, region IDs or response bodies.
            fact={'target':target,'error_type':diagnostics.identifier(result.get('error_type'))}
            if result.get('endpoint') in {'regions','capabilities','prediction-window','forecast-dashboard','model-quality','feature-importance'}:
                fact['endpoint']=result['endpoint']
            if type(result.get('http_status')) is int and 100<=result['http_status']<=599:
                fact['http_status']=result['http_status']
            self.save(probe_failure=fact)
            raise RuntimeError('Read-only business verification failed: '+json.dumps(fact,sort_keys=True))

    def public_base(self):
        base=os.environ.get('PUBLIC_API_BASE_URL','')
        parsed=urlsplit(base)
        if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path.rstrip('/')!='/api/v1'):
            raise ValueError('Set the reviewed frontend HTTPS PUBLIC_API_BASE_URL ending in /api/v1')
        return base.rstrip('/')

    def public_health(self):
        script=Path(__file__).with_name('public_probe.py').read_text(encoding='utf-8')
        script+='\nprint(json.dumps(check('+repr(self.public_base())+')))'
        proof=self.remote(self.api(),script)
        self.save(business_proofs=[*self.state.get('business_proofs',[]),{'target':'public_ingress',**proof}])
        return proof

    def monitor_start(self):
        stop=threading.Event(); result={'samples':0,'requests':0,'errors':0,'started_at':now()}
        script=Path(__file__).with_name('role_probe.py').read_text(encoding='utf-8').split("if __name__ == '__main__':")[0]
        script+='\nprint(json.dumps(run_check(\'http://traffic-api:8000/api/v1\',light=True)))'
        public_script=Path(__file__).with_name('public_probe.py').read_text(encoding='utf-8')
        public_script+='\nprint(json.dumps(check('+repr(self.public_base())+')))'
        result['targets']=['internal_service','public_ingress']
        def observe():
            while not stop.is_set():
                try:
                    with self.probe_lock():
                        proof=self.cluster.remote(self.api(),script)
                        public=self.cluster.remote(self.api(),public_script)
                    if proof.get('probe_failed') or proof.get('errors')!=0:
                        result['last_probe_failure']={k:proof[k] for k in ('error_type','endpoint','http_status') if k in proof}
                        raise RuntimeError('Availability business probe failed')
                    result['samples']+=1; result['requests']+=proof['requests']+public['requests']
                except Exception as error:
                    result['errors']+=1
                    result['last_error_type']=type(error).__name__
                stop.wait(10)
        thread=threading.Thread(target=observe,daemon=True,name='read-only-availability')
        thread.start()
        self.monitor=(stop,thread,result)

    def monitor_check(self):
        if hasattr(self,'monitor') and self.monitor[2]['errors']:
            raise RuntimeError('Availability observation failed; release is not verified')

    def monitor_stop(self):
        if not hasattr(self,'monitor'):
            return
        stop,thread,result=self.monitor
        stop.set(); thread.join(50)
        if thread.is_alive():
            result['errors']+=1; result['last_error_type']='ObservationTimeout'
        self.save(availability={**result,'finished_at':now()})
        self.monitor_check()

    def guard_status(self):
        return self.cluster.call('exec',self.state['guard_pod'],'--','python','-B','-c',STATUS)

    def flag(self, name, value):
        if name not in {'peer','release'}:
            raise ValueError('Unexpected lease flag')
        code = "from pathlib import Path;p=Path('/tmp/role-guard-"+name+"');" + ('p.touch()' if value else 'p.unlink(missing_ok=True)')
        self.cluster.call('exec',self.state['guard_pod'],'--','python','-B','-c',code,json_output=False)

    def peer(self, value):
        self.flag('peer',value)
        deadline = time.monotonic()+120
        while time.monotonic()<deadline:
            status = self.guard_status()
            if status.get('state')!='held':
                raise RuntimeError('Deployment lease was lost')
            if status.get('peer_held') is value:
                return
            time.sleep(.5)
        raise RuntimeError('Peer transition lease did not settle')

    def start_guard(self):
        pod = self.api()['spec']['template']['spec']; main=container(self.api())[1]
        env = [v for v in main.get('env',[]) if 'valueFrom' in v]
        name = 'delivery-'+self.release['release_id'][:10]+'-'+str(int(time.time()))
        job = {'apiVersion':'batch/v1','kind':'Job','metadata':{'name':name,'namespace':'traffic-mlops'},
               'spec':{'backoffLimit':0,'activeDeadlineSeconds':3000,'ttlSecondsAfterFinished':3600,
                       'template':{'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,
                          'imagePullSecrets':pod.get('imagePullSecrets',[]),
                          'containers':[{'name':'guard','image':self.images['sync']['image'],
                            'command':['python','-B','/app/scripts/cicd/roles_guard.py'],
                            'envFrom':main.get('envFrom',[]),'env':env,
                            'resources':{'requests':{'cpu':'10m','memory':'64Mi'},'limits':{'cpu':'250m','memory':'256Mi'}}}]}}}}
        self.save(guard_job=name)
        self.cluster.call('create','-f','-','-o','json',value=job)
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            pods=self.cluster.call('get','pods','-l','job-name='+name,'-o','json')['items']
            if pods:
                self.save(guard_pod=pods[0]['metadata']['name'])
                if pods[0].get('status',{}).get('phase') in {'Failed','Succeeded'}:
                    raise RuntimeError('Deployment admission lease could not start')
                if pods[0].get('status',{}).get('phase')=='Running':
                    try:
                        if self.guard_status().get('state')=='held':
                            self.save(guard_held=True)
                            return
                    except RuntimeError:
                        pass
            time.sleep(2)
        raise RuntimeError('Deployment admission lease timed out')

    def release_guard(self):
        if self.guard_status().get('state')!='held':
            raise RuntimeError('Deployment admission lease was lost')
        self.flag('release',True)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            # The process exits after writing released. Job completion is proof
            # the DB connection is closed, including every advisory lock.
            job=self.get('job',self.state['guard_job'])
            if any(c['type']=='Complete' and c['status']=='True' for c in job.get('status',{}).get('conditions',[])):
                self.save(guard_held=False)
                return
            time.sleep(1)
        raise RuntimeError('Admission lease release was not confirmed')

    def endpoints(self, service, app, count):
        pods=ready_pods(self.cluster.deployment(app),self.cluster.call('get','pods','-o','json')['items'])
        expected={p['status']['podIP'] for p in pods}
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            value=self.get('endpoints',service)
            actual={a['ip'] for s in value.get('subsets',[]) for a in s.get('addresses',[])}
            if len(expected)==count and actual==expected:
                return
            time.sleep(1)
        raise RuntimeError('Service endpoints did not match the ready cohort')

    def plan(self):
        self.public_base()
        if self.get('namespace','traffic-mlops')['metadata']['uid']!=self.cluster.namespace_uid:
            raise RuntimeError('Wrong cluster namespace')
        api=self.api(); active=api['metadata']['name']
        idle=self.remote(api,IDLE)
        if idle['heads']!=self.release['baseline']['migration_heads']:
            raise RuntimeError('Unexpected migration state')
        changes={}; targets={}; originals={}
        for role,first,second,service in [('api','api','api-next','traffic-api'),('tree','train-tree','tree-next','traffic-training-tree'),
                                         ('neural','train-neural','neural-next','traffic-training-worker')]:
            old=self.get('service',service)['spec']['selector']['app']
            source=self.cluster.deployment(old)
            originals[role]=old
            legacy=bool(self.migration and self.migration.legacy(old,role))
            if not legacy and old not in SLOTS[role]:
                raise ValueError('Runtime migration required for '+role)
            if not legacy:
                # Failure to observe the current version is never a reason to
                # deploy another copy. A changed desired spec is a separate fact.
                self.health(old,role,target=False)
                wanted=render(role,old,self.images,directory=self.directory)['spec']
                normalized=self.patch('deployment',source,[{'op':'replace','path':'/spec','value':wanted}],dry=True)
                unchanged=source['spec']==normalized['spec']
            else:
                unchanged=False
            if unchanged:
                targets[role]=old
                continue
            name=second if old==first else first
            targets[role]=name
            current=self.cluster.deployment(name)
            if current['spec'].get('replicas',0) and current['metadata']['name']!=source['metadata']['name']:
                raise RuntimeError('Inactive cohort is not empty')
            wanted=render(role,name,self.images,directory=self.directory)['spec']
            # Server defaulting is part of the persisted after hash.
            dry=self.patch('deployment',current,[{'op':'replace','path':'/spec','value':wanted}],dry=True)
            changes[name]=dry['spec']
        if self.migration:
            self.migration.preflight(originals)
        airflow_changed=self.airflow_changed()
        components=[]
        for role in ('api','tree','neural'):
            components.append({'component':role,'action':'deploy' if targets[role] in changes else 'keep',
                               'reason':'image_or_manifest_changed' if targets[role] in changes else 'desired_state_unchanged'})
        components.append({'component':'airflow','action':'deploy' if airflow_changed else 'keep',
                           'reason':'dag_source_changed' if airflow_changed else 'desired_state_unchanged'})
        self.save(components=components)
        if changes or airflow_changed:
            self.capacity(api,changes,airflow_changed)
        if targets['api'] in changes and not idle['active'] and not idle['running']:
            self.restore_preflight(api)
        return {'active':active,'target':targets['api'],'targets':targets,'originals':originals,
                'changes':changes,'airflow_changed':airflow_changed,'idle':idle}

    def restore_preflight(self, api):
        # Execute the checked candidate's listing rules on an existing sidecar.
        # This works before upgrading the sync image and never downloads/deletes.
        candidate = (self.directory / 'roles/sync/backend/app/services/restore_cache.py').read_text(encoding='utf-8')
        pod=json.loads((self.directory/'deploy/api.json').read_bytes())['spec']['template']['spec']
        init=next(c for c in pod['initContainers'] if c['name']=='restore-cache')
        mode=next((e['value'] for e in init.get('env',[]) if e['name']=='TRAFFIC_CACHE_RESTORE_MODE'),'full')
        source = ("import json,os\nns={'__name__':'restore_preflight'}\nexec(" + repr(candidate) + ",ns)\n"
                  "from backend.app.services.region_cleanup_uploader import get_upload_client\n"
                  "print(json.dumps(ns['inventory_report'](get_upload_client(),os.environ['TRAFFIC_API_RUNTIME_BUCKET'],"
                  "required_keys=ns['restore_keys']("+repr(mode)+"))))")
        sidecar='artifact-sync'
        if getattr(self,'migration',None) and self.migration.legacy(api['metadata']['name'],'api'):
            source=self.migration.source('inventory')
            sidecar=self.migration.plan['api_sidecar']
        report = self.execute(self.cluster.pod(api), sidecar, source)
        self.save(restore_inventory=report)
        if report.get('complete') is not True or report.get('within_budget') is not True:
            raise RuntimeError('Artifact cache preflight exceeded the reviewed restore budget; inspect inventory counts')

    def capacity(self,api,changes,airflow_changed):
        from scripts.cicd.capacity import verify
        pods=self.cluster.call('get','pods','-o','json')['items']
        hosts={}
        source="import os,json;from pathlib import Path;m={r.split(':')[0]:int(r.split()[1])*1024 for r in Path('/proc/meminfo').read_text().splitlines() if r.startswith('MemAvailable:')};s=os.statvfs('/app/models');print(json.dumps({'cpus':os.cpu_count(),'memory_available':m['MemAvailable'],'disk_available':s.f_bavail*s.f_frsize}))"
        for pod in ready_pods(api,pods):
            hosts[pod['spec']['nodeName']]=self.execute(pod['metadata']['name'],container(api)[1]['name'],source)
        if len(hosts)!=2:raise RuntimeError('Serving replicas are not distributed across two known nodes')
        assignments=[]
        for name,spec in changes.items():
            pod=spec['template']['spec']
            if name in SLOTS['api']:
                assignments.extend((node,pod) for node in hosts)
            else:
                node=pod.get('nodeSelector',{}).get('kubernetes.io/hostname')
                assignments.append((node,pod))
        if airflow_changed:
            scheduler=self.cluster.deployment('airflow-scheduler')
            if scheduler['spec'].get('strategy',{}).get('type','RollingUpdate')!='Recreate':
                for pod in ready_pods(scheduler,pods):assignments.append((pod['spec']['nodeName'],scheduler['spec']['template']['spec']))
        self.save(capacity=verify(pods,assignments,hosts,minimum_disk_bytes=self.contract['capacity']['minimum_disk_bytes']))

    def airflow_changed(self):
        scheduler=self.cluster.deployment('airflow-scheduler')
        if container(scheduler)[1]['image']!=self.release['baseline']['airflow_image']:
            raise RuntimeError('Airflow dependency image differs')
        actual=self.remote(scheduler,AIRFLOW_FILES)
        expected={k:digest((self.directory/'airflow'/k).read_bytes()) for k in actual}
        return actual!=expected

    def airflow(self):
        files={'dag.py':(self.directory/'airflow/dag.py').read_text(encoding='utf-8'),
               'airflow_control.py':(self.directory/'airflow/airflow_control.py').read_text(encoding='utf-8'),
               'legacy.py':(self.directory/'airflow/legacy/traffic_region_training_worker.py').read_text(encoding='utf-8')}
        name='role-airflow-'+digest(canonical(files))[:16]
        cm={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':'traffic-mlops'},'immutable':True,'data':files}
        result=self.cluster.call('get','configmap',name,'--ignore-not-found','-o','json')
        if result and (result.get('data')!=files or result.get('immutable') is not True):
            raise RuntimeError('Immutable Airflow source differs')
        if not result:
            self.cluster.call('create','-f','-','-o','json',value=cm)
        # Only LocalExecutor's scheduler executes these helpers. The webserver
        # stays live to acknowledge durable queued requests throughout delivery.
        executor=self.remote(self.cluster.deployment('airflow-scheduler'),
            "import json;from airflow.configuration import conf;print(json.dumps({'executor':conf.get('core','executor')}))")
        if executor.get('executor')!='LocalExecutor':
            raise RuntimeError('This delivery requires the reviewed LocalExecutor topology')
        for target in ('airflow-scheduler',):
            value=self.cluster.deployment(target); spec=copy.deepcopy(value['spec'])
            pod=spec['template']['spec']; main=container(value)[1]
            if main['image']!=self.release['baseline']['airflow_image']:
                raise RuntimeError('Airflow dependency image differs')
            mounts=[m for m in main.get('volumeMounts',[]) if m['mountPath']=='/opt/traffic-cloud']
            if len(mounts)!=1:
                raise RuntimeError('Unknown Airflow source mount')
            for volume in pod['volumes']:
                if volume['name']==mounts[0]['name']:
                    volume.clear(); volume.update(name=mounts[0]['name'],configMap={'name':name,
                        'items':[{'key':'dag.py','path':'dag.py'},{'key':'airflow_control.py','path':'airflow_control.py'},
                                 {'key':'legacy.py','path':'legacy/traffic_region_training_worker.py'}]})
            pod['initContainers']=[c for c in pod.get('initContainers',[]) if c['name']!='delivery-source']
            self.change('deployment',target,spec)
            self.cluster.wait(target)
            actual=self.remote(self.cluster.deployment(target),AIRFLOW_FILES)
            expected={k:digest((self.directory/'airflow'/k).read_bytes()) for k in actual}
            if actual!=expected or not self.remote(self.cluster.deployment(target),DAG_CHECK).get('dag_valid'):
                raise RuntimeError('Airflow source/graph verification failed')
        if not self.remote(self.cluster.deployment('airflow-webserver'),DAG_COMPATIBILITY_CHECK).get('dag_valid'):
            raise RuntimeError('Live Airflow webserver graph compatibility failed')

    def retire_api(self,name):
        if self.migration and self.migration.legacy(name,'api'):
            self.migration.retire_api(name)
        else:
            self.drain_managed(name)

    def drain_managed(self,name,*,scale=True):
        value=self.cluster.deployment(name)
        pods=ready_pods(value,self.cluster.call('get','pods','-o','json')['items'])
        if len(pods)!=value['spec']['replicas']:
            raise RuntimeError('Cannot confirm candidate drain/cache state; keep the cohort for recovery')
        for pod in pods:
            source="import json,os;from urllib.request import Request,urlopen;r=Request('http://127.0.0.1:8000/api/v1/internal/lifecycle/drain',data=b'',headers={'X-Internal-Token':os.environ['INTERNAL_TRAINING_TOKEN']},method='POST');print(json.dumps(json.load(urlopen(r,timeout=10))))"
            self.execute(pod['metadata']['name'],'api',source)
            source="import json,os;from urllib.request import Request,urlopen;r=Request('http://127.0.0.1:8000/api/v1/internal/lifecycle/status',headers={'X-Internal-Token':os.environ['INTERNAL_TRAINING_TOKEN']});print(json.dumps(json.load(urlopen(r,timeout=10))))"
            deadline=time.monotonic()+840
            time.sleep(10)
            while time.monotonic()<deadline:
                if self.execute(pod['metadata']['name'],'api',source)['inflight']==0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('API requests have not drained; retirement stopped')
            flush="from backend.app.services.artifact_sync_control import main;main(['flush'])"
            self.require_flush(self.execute(pod['metadata']['name'],'artifact-sync',flush,timeout=650),name)
        if scale:self.scale(name,0)

    def wait_retired(self,name):
        selector=self.cluster.deployment(name)['spec']['selector']['matchLabels']
        deadline=time.monotonic()+950
        while time.monotonic()<deadline:
            pods=self.cluster.call('get','pods','-o','json')['items']
            if not any(all(p['metadata'].get('labels',{}).get(k)==v for k,v in selector.items()) for p in pods):
                return
            time.sleep(2)
        raise RuntimeError('Retired cohort has not finished graceful shutdown')

    def verify(self):
        active=self.api()['metadata']['name']
        tree=self.get('service','traffic-training-tree')['spec']['selector']['app']
        neural=self.get('service','traffic-training-worker')['spec']['selector']['app']
        for role,name in (('api',active),('tree',tree),('neural',neural)):
            if name not in SLOTS[role]:raise RuntimeError('The requested role runtime is not deployed')
            value=self.cluster.deployment(name)
            wanted=render(role,name,self.images,directory=self.directory)['spec']
            normalized=self.patch('deployment',value,[{'op':'replace','path':'/spec','value':wanted}],dry=True)
            if value['spec']!=normalized['spec']:
                raise RuntimeError('The running role manifest differs from the requested release')
        if self.airflow_changed():raise RuntimeError('Airflow source differs from the requested release')
        proof=self.health(active,'api')+self.health(tree,'tree')+self.health(neural,'neural')
        for service,app,count in [('traffic-api',active,2),('traffic-api-training',active,2),('api-peers',active,2),
                                  ('traffic-training-tree',tree,1),('traffic-training-worker',neural,1)]:
            self.endpoints(service,app,count)
        self.business()
        self.public_health()
        return proof

    def rollback(self):
        if self.state.get('completed') and not self.state.get('recovery_required'):
            raise RuntimeError('A completed release requires a new reviewed rollback release')
        if self.state.get('guard_held') and self.guard_status().get('state')!='held':
            raise RuntimeError('Lease lost; automatic inverse is unsafe')
        # Reversal is exact and ordered: restore old cohorts before routing back,
        # then remove staged cohorts. No inverse touches application data.
        old=self.state.get('active_before')
        exposed=self.get('service','traffic-api')['spec']['selector']['app'] if old else None
        if old in {'api','api-next'}:
            value=self.cluster.deployment(old)
            labels=value['spec']['selector']['matchLabels']
            for pod in self.cluster.call('get','pods','-o','json')['items']:
                if (all(pod['metadata'].get('labels',{}).get(k)==v for k,v in labels.items())
                        and not pod['metadata'].get('deletionTimestamp') and pod.get('status',{}).get('phase')=='Running'):
                    source="import json,os;from urllib.request import Request,urlopen;r=Request('http://127.0.0.1:8000/api/v1/internal/lifecycle/resume',data=b'',headers={'X-Internal-Token':os.environ['INTERNAL_TRAINING_TOKEN']},method='POST');print(json.dumps(json.load(urlopen(r,timeout=10))))"
                    self.cluster.call('exec',pod['metadata']['name'],'-c','api','--','python','-B','-c',source)
            if value['spec'].get('replicas',0):
                self.cluster.wait(old)
        peer_held=False
        for plan in reversed(self.inverses):
            if self.state.get('guard_held') and (plan['kind']=='service') != peer_held:
                peer_held=plan['kind']=='service'
                self.peer(peer_held)
            value=self.get(plan['kind'],plan['name'])
            if value['metadata']['uid']!=plan['uid']:
                raise RuntimeError('Resource ownership changed during delivery')
            if value['spec']==plan['before']:
                continue
            if digest(canonical(value['spec']))!=plan['after']:
                raise RuntimeError('External resource drift prevents automatic inverse')
            if (plan['kind']=='deployment' and plan['before'].get('replicas',0)==0
                    and plan['name']==exposed and exposed!=old and exposed in SLOTS['api']):
                # Old traffic has already been restored by the preceding
                # inverse Service operations. Finish candidate requests/cache
                # too; never append a scale inverse while reversing another.
                self.drain_managed(exposed,scale=False)
            self.patch(plan['kind'],value,[{'op':'replace','path':'/spec','value':plan['before']}])
            if plan['kind']=='deployment':
                self.cluster.wait(plan['name'])
                if plan['before'].get('replicas',0)==0:self.wait_retired(plan['name'])
        if peer_held:
            self.peer(False)
        self.pause(self.state['dag_was_paused'])
        self.pause(self.state['drift_was_paused'],drift=True)
        if self.state.get('guard_held'):
            self.release_guard()
        elif self.state.get('guard_job'):
            job=self.cluster.call('get','job',self.state['guard_job'],'--ignore-not-found','-o','json')
            if job and not any(c['type'] in {'Complete','Failed'} and c['status']=='True' for c in job.get('status',{}).get('conditions',[])):
                if not self.state.get('guard_pod'):
                    raise RuntimeError('The admission job has an uncertain startup; recovery is required')
                self.flag('release',True)
                self.cluster.call('wait','--for=condition=complete','job/'+self.state['guard_job'],'--timeout=30s',json_output=False)
        self.resource_proof()
        self.save(rolled_back=True,recovery_required=False,recovered_at=now(),phase='rolled_back')

    def resource_proof(self):
        names={(p['kind'],p['name']) for p in self.inverses}
        names.update(('service',name) for name in SERVICES)
        names.update(('deployment',self.get('service',name)['spec']['selector']['app'])
                     for name in ('traffic-api','traffic-training-tree','traffic-training-worker'))
        names.update(('deployment',name) for name in ('airflow-scheduler','airflow-webserver'))
        proofs=[]
        for kind,name in sorted(names):
            value=self.get(kind,name)
            proofs.append({'kind':kind,'name':name,'uid':value['metadata']['uid'],'spec_sha256':digest(canonical(value['spec']))})
        self.save(resources=proofs)

    def apply(self):
        if self.state.get('started'):
            raise RuntimeError('Do not replay an existing role transaction; inspect recovery')
        self.save(phase='plan')
        plan=self.plan()
        # No-op is a desired-state comparison, never an exception fallback.
        if not plan['changes'] and not plan.get('airflow_changed',False):
            proof=self.verify()
            self.resource_proof()
            self.save(completed=True,up_to_date=True,phase='completed',proof=proof,dag_was_paused=plan['idle']['paused'],drift_was_paused=plan['idle']['drift_paused'])
            return
        if plan['idle']['active'] or plan['idle']['running']:
            self.save(deferred=True,reason='active_training',retry_ci_run_id=os.environ.get('CI_RUN_ID'))
            return
        self.save(started=True,preflight_only=False,dag_was_paused=plan['idle']['paused'],
                  drift_was_paused=plan['idle']['drift_paused'],
                  active_before=plan['active'],target=plan['target'])
        try:
            self.save(phase='admission')
            self.pause(True)
            self.pause(True,drift=True)
            idle=self.remote(self.api(),IDLE)
            if idle['active'] or idle['running']:
                self.pause(plan['idle']['paused'])
                self.pause(plan['idle']['drift_paused'],drift=True)
                self.save(started=False,preflight_only=True,deferred=True,reason='training_started_before_fence',retry_ci_run_id=os.environ.get('CI_RUN_ID'))
                return
            self.start_guard()
            self.business()
            self.public_health()
            self.monitor_start()
            self.save(phase='stage_api')
            if plan['target'] in plan['changes']:
                self.restore_preflight(self.api())
                self.change('deployment',plan['target'],plan['changes'][plan['target']])
                self.cluster.wait(plan['target'])
            self.health(plan['target'],'api')
            self.route('api-preview',plan['target']); self.endpoints('api-preview',plan['target'],2)
            self.business('api-preview')
            self.monitor_check()
            self.save(phase='stage_workers')
            for role in ('tree','neural'):
                name=plan['targets'][role]
                if name in plan['changes']:
                    self.change('deployment',name,plan['changes'][name]); self.cluster.wait(name)
                self.health(name,role)
            if plan.get('airflow_changed',True):
                self.save(phase='update_airflow')
                self.airflow()
            self.monitor_check()
            self.save(phase='promote')
            self.peer(True)
            for service,app,count in [('traffic-training-tree',plan['targets']['tree'],1),('traffic-training-worker',plan['targets']['neural'],1),
                                      ('api-peers',plan['target'],2),('traffic-api-training',plan['target'],2),('traffic-api',plan['target'],2)]:
                self.route(service,app); self.endpoints(service,app,count)
            self.peer(False)
            self.save(phase='verify_promoted')
            proof=self.verify()
            # Allow kube-proxy/ingress endpoint propagation before draining.
            time.sleep(15)
            self.save(phase='retire')
            if plan['active']!=plan['target']:
                self.retire_api(plan['active'])
                self.wait_retired(plan['active'])
            # Retire the legacy idle watchers before allowing queued dispatch.
            for role in ('tree','neural'):
                old=plan['originals'][role]
                if old!=plan['targets'][role] and self.cluster.deployment(old)['spec'].get('replicas',0):
                    self.scale(old,0); self.wait_retired(old)
            self.business()
            self.monitor_stop()
            self.save(phase='release_admission')
            self.pause(plan['idle']['paused'])
            self.pause(plan['idle']['drift_paused'],drift=True)
            self.release_guard()
            self.resource_proof()
            self.save(completed=True,recovery_required=False,phase='completed',proof=proof)
        except Exception as error:
            self.save(recovery_required=True,failed_at=now(),failed_phase=self.state.get('phase'),error_type=type(error).__name__,phase='rollback')
            try:
                self.monitor_stop()
            except Exception:
                pass
            try:
                evidence=diagnostics.collect(self.cluster,self.state['patched'])
                self.path.with_suffix('.diagnostics.json').write_text(json.dumps(evidence),encoding='utf-8')
            except Exception:
                pass
            try:
                self.rollback()
            except Exception:
                self.save(recovery_required=True)
            raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['Plan','Apply','Verify','Rollback'])
    for name in ('directory','binding','commit','journal','kubectl','context','namespace-uid','kubeconfig'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--migration',choices=['role-runtime-v1'])
    args=parser.parse_args()
    op=RolesDelivery(Cluster(args.kubectl,args.context,args.namespace_uid,args.kubeconfig),args.directory,args.binding,
                     args.journal,commit=args.commit,kubeconfig=args.kubeconfig,migration=args.migration)
    try:
        if args.action=='Plan':
            plan=op.plan()
            print(json.dumps({'server_validated_not_applied':True,'active':plan['active'],'target':plan['target'],'training_idle':not(plan['idle']['active'] or plan['idle']['running'])}))
        elif args.action=='Apply':
            op.apply()
            print(json.dumps({k:op.state.get(k,False) for k in ('completed','up_to_date','deferred','rolled_back','recovery_required')}))
        elif args.action=='Verify':
            if op.state.get('deferred'):
                print(json.dumps({'deferred':True,'deployed':False}))
            else:
                print(json.dumps({'proof':op.verify()}))
        else:
            op.rollback()
            print(json.dumps({k:op.state.get(k,False) for k in ('rolled_back','guard_held','recovery_required')}))
    except Exception as error:
        op.save(error_type=type(error).__name__,failed_phase=op.state.get('failed_phase',op.state.get('phase','preflight')))
        if args.action=='Verify' and op.state.get('completed'):
            op.save(completed=False,recovery_required=True,failed_at=now(),phase='verify_failed_after_release')
        print(json.dumps({'delivery_failed':True,'error_type':type(error).__name__,
                          'reason':str(error) if isinstance(error,(ValueError,RuntimeError)) else 'Details omitted.',
                          'recovery_required':op.state.get('recovery_required',True)}))
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
