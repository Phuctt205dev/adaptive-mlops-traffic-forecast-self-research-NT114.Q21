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
from scripts.cicd.deploy import Cluster, container, ready_pods, now, spec_hash, AIRFLOW_FILES, DAG_CHECK
from scripts.cicd.release import canonical, digest, DIGEST_IMAGE
from scripts.cicd.roles_release import verify, ROLES
from scripts.cicd.role_blueprints import deployment as blueprint
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
    def __init__(self, cluster, directory, binding, journal, *, commit, kubeconfig):
        self.cluster, self.directory, self.path = cluster, Path(directory), Path(journal)
        self.release = verify(directory, expected_commit=commit)
        self.images = validate_delivery(self.release, json.loads(Path(binding).read_text(encoding='utf-8-sig')))
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

    def save(self, **values):
        self.state.update(values, updated_at=now())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        p = self.path.with_suffix('.tmp')
        p.write_text(json.dumps(self.state, indent=2), encoding='utf-8')
        p.replace(self.path)

    def get(self, kind, name):
        return self.cluster.call('get',kind,name,'-o','json')

    def change(self, kind, name, spec):
        """Write-ahead inverse + exact after hash, then version-checked replace."""
        current = self.get(kind,name)
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
        tests = [{'op':'test','path':'/metadata/uid','value':current['metadata']['uid']},
                 {'op':'test','path':'/metadata/resourceVersion','value':current['metadata']['resourceVersion']}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'patch.json'
            path.write_text(json.dumps(tests+operations),encoding='utf-8')
            args = ['patch',kind,current['metadata']['name'],'--type=json','--patch-file='+str(path),'-o','json']
            if dry:
                args.append('--dry-run=server')
            return self.cluster.call(*args)

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
        if app not in {'traffic-api','api','api-next'}:
            raise RuntimeError('Unknown active API cohort')
        return self.cluster.deployment(app)

    def pause(self, value=None, *, drift=False):
        expression = ('c.get(base,auth=auth,timeout=10)' if value is None else
                      'c.patch(base,json={"is_paused":'+repr(value)+'},auth=auth,timeout=10)')
        code = ("import requests,json;from backend.app.core.config import get_settings;s=get_settings();"
                "c=requests.Session();c.trust_env=False;base=s.airflow_api_url+'/dags/'+s."+('drift_dag_id' if drift else 'training_dag_id')+";"
                "auth=(s.airflow_admin_username,s.airflow_admin_password);r="+expression+
                ";r.raise_for_status();print(json.dumps({'paused':r.json()['is_paused']}))")
        actual = self.cluster.remote(self.api(),code)['paused']
        if value is not None and actual is not value:
            raise RuntimeError('DAG pause state was not confirmed')
        return actual

    def health(self, name, role):
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
            binding = self.images[role]
            if (actual.get('source_id') != binding['source_id'] or actual.get('commit') != binding['commit']
                    or actual.get('image_role') != role or main['image'] != binding['image']):
                raise RuntimeError('Running role differs from its immutable binding')
            if role == 'api':
                sync = [c for c in pod['spec'].get('initContainers',[]) if c['name']=='artifact-sync']
                statuses = pod['status'].get('initContainerStatuses',[])
                if len(sync)!=1 or sync[0]['image']!=self.images['sync']['image'] or not any(s['name']=='artifact-sync' and s.get('ready') for s in statuses):
                    raise RuntimeError('The native artifact sidecar is not ready')
                check = "from deployment.rke2.runtime.role_runtime import verify;import json;d=verify();print(json.dumps({'source_id':d['source_id']}))"
                result = self.cluster.call('exec',pod['metadata']['name'],'-c','artifact-sync','--','python','-B','-c',check)
                if result.get('source_id')!=self.images['sync']['source_id']:
                    raise RuntimeError('Sidecar source identity differs')
            proofs.append({'deployment':name,'pod':pod['metadata']['name'],'role':role,'source_id':actual['source_id'],'verified':True})
        return proofs

    def business(self, host='traffic-api'):
        script = (Path(__file__).with_name('role_probe.py')).read_text(encoding='utf-8')
        script = script.split("if __name__ == '__main__':")[0] + '\nprint(json.dumps(check('+repr('http://'+host+':8000/api/v1')+')))'
        result = self.cluster.remote(self.api(),script)
        self.save(business_proofs=[*self.state.get('business_proofs',[]),{'target':host,**result}])
        return result

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
        proof=self.cluster.remote(self.api(),script)
        self.save(business_proofs=[*self.state.get('business_proofs',[]),{'target':'public_ingress',**proof}])
        return proof

    def monitor_start(self):
        stop=threading.Event(); result={'samples':0,'requests':0,'errors':0,'started_at':now()}
        script=Path(__file__).with_name('role_probe.py').read_text(encoding='utf-8').split("if __name__ == '__main__':")[0]
        script+='\nprint(json.dumps(check(\'http://traffic-api:8000/api/v1\',light=True)))'
        public_script=Path(__file__).with_name('public_probe.py').read_text(encoding='utf-8')
        public_script+='\nprint(json.dumps(check('+repr(self.public_base())+')))'
        result['targets']=['internal_service','public_ingress']
        def observe():
            while not stop.is_set():
                try:
                    proof=self.cluster.remote(self.api(),script)
                    public=self.cluster.remote(self.api(),public_script)
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
        idle=self.cluster.remote(api,IDLE)
        if idle['heads']!=self.release['baseline']['migration_heads']:
            raise RuntimeError('Unexpected migration state')
        target='api-next' if active=='api' else 'api'
        changes={}; targets={}; originals={}
        for role,first,second,service in [('api','api','api-next','traffic-api'),('tree','train-tree','tree-next','traffic-training-tree'),
                                         ('neural','train-neural','neural-next','traffic-training-worker')]:
            old=self.get('service',service)['spec']['selector']['app']
            source=self.cluster.deployment(old)
            originals[role]=old
            try:
                self.health(old,role)
                targets[role]=old
                continue
            except RuntimeError:
                pass
            name=second if old==first else first
            targets[role]=name
            current=self.cluster.deployment(name)
            if current['spec'].get('replicas',0) and current['metadata']['name']!=source['metadata']['name']:
                raise RuntimeError('Inactive cohort is not empty')
            wanted=blueprint(source,name,role,self.images,self.images[role]['source_id'])['spec']
            wanted['replicas']=2 if role=='api' else 1
            # Server defaulting is part of the persisted after hash.
            dry=self.patch('deployment',current,[{'op':'replace','path':'/spec','value':wanted}],dry=True)
            changes[name]=dry['spec']
        # Preserve capacity requests; reject low disk headroom before staging.
        for pod in ready_pods(api,self.cluster.call('get','pods','-o','json')['items']):
            source="import os,json;s=os.statvfs('/app/models');print(json.dumps({'available':s.f_bavail*s.f_frsize}))"
            available=self.cluster.call('exec',pod['metadata']['name'],'-c',container(api)[1]['name'],'--','python','-B','-c',source)['available']
            if available < 12*1024**3:
                raise RuntimeError('A serving node lacks staging disk headroom')
        return {'active':active,'target':targets['api'],'targets':targets,'originals':originals,'changes':changes,'idle':idle}

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
        executor=self.cluster.remote(self.cluster.deployment('airflow-scheduler'),
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
            actual=self.cluster.remote(self.cluster.deployment(target),AIRFLOW_FILES)
            expected={k:digest((self.directory/'airflow'/k).read_bytes()) for k in actual}
            if actual!=expected or not self.cluster.remote(self.cluster.deployment(target),DAG_CHECK).get('dag_valid'):
                raise RuntimeError('Airflow source/graph verification failed')
        if not self.cluster.remote(self.cluster.deployment('airflow-webserver'),DAG_CHECK).get('dag_valid'):
            raise RuntimeError('Live Airflow webserver graph compatibility failed')

    def drain_legacy(self, name):
        """Wait for all old TCP sessions, then flush its cache before retirement.

        Includes probes/keep-alive sessions rather than guessing which are idle.
        No process is killed if a long request exceeds the drain budget.
        """
        value=self.cluster.deployment(name)
        pods=ready_pods(value,self.cluster.call('get','pods','-o','json')['items'])
        source="import json;from pathlib import Path;rows=[line.split() for file in ('/proc/net/tcp','/proc/net/tcp6') for line in Path(file).read_text().splitlines()[1:]];print(json.dumps({'connections':sum(r[1].endswith(':1F40') and r[3]=='01' for r in rows)}))"
        for pod in pods:
            deadline=time.monotonic()+900; quiet_since=None
            while time.monotonic()<deadline:
                count=self.cluster.call('exec',pod['metadata']['name'],'-c',container(value)[1]['name'],'--','python','-B','-c',source)['connections']
                quiet_since=quiet_since or time.monotonic() if count==0 else None
                if quiet_since is not None and time.monotonic()-quiet_since>=20:
                    break
                time.sleep(2)
            else:
                raise RuntimeError('Legacy API still has live connections; retirement stopped')
            flush="import importlib.util,json;s=importlib.util.spec_from_file_location('role_flush','/opt/region-cleanup/region_cleanup_uploader.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(json.dumps({'flushed':m.upload_cycle()}))"
            self.cluster.call('exec',pod['metadata']['name'],'-c','artifact-cache-sync','--','python','-B','-c',flush,timeout=300)
        self.save(legacy_drain={'connections_quiet_seconds':20,'cache_flush_confirmed':True})
        self.scale(name,0)

    def drain_managed(self,name):
        value=self.cluster.deployment(name)
        pods=ready_pods(value,self.cluster.call('get','pods','-o','json')['items'])
        for pod in pods:
            source="import json,os;from urllib.request import Request,urlopen;r=Request('http://127.0.0.1:8000/api/v1/internal/lifecycle/drain',data=b'',headers={'X-Internal-Token':os.environ['INTERNAL_TRAINING_TOKEN']},method='POST');print(json.dumps(json.load(urlopen(r,timeout=10))))"
            self.cluster.call('exec',pod['metadata']['name'],'-c','api','--','python','-B','-c',source)
            source="import json,os;from urllib.request import Request,urlopen;r=Request('http://127.0.0.1:8000/api/v1/internal/lifecycle/status',headers={'X-Internal-Token':os.environ['INTERNAL_TRAINING_TOKEN']});print(json.dumps(json.load(urlopen(r,timeout=10))))"
            deadline=time.monotonic()+840
            time.sleep(10)
            while time.monotonic()<deadline:
                if self.cluster.call('exec',pod['metadata']['name'],'-c','api','--','python','-B','-c',source)['inflight']==0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('API requests have not drained; retirement stopped')
            flush="import json;from backend.app.services.region_cleanup_uploader import upload_cycle;print(json.dumps({'flushed':upload_cycle()}))"
            self.cluster.call('exec',pod['metadata']['name'],'-c','artifact-sync','--','python','-B','-c',flush,timeout=300)
        self.scale(name,0)

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
        proof=self.health(active,'api')+self.health(tree,'tree')+self.health(neural,'neural')
        for service,app,count in [('traffic-api',active,2),('traffic-api-training',active,2),('api-peers',active,2),
                                  ('traffic-training-tree',tree,1),('traffic-training-worker',neural,1)]:
            self.endpoints(service,app,count)
        self.business()
        self.public_health()
        return proof

    def rollback(self):
        if self.state.get('guard_held') and self.guard_status().get('state')!='held':
            raise RuntimeError('Lease lost; automatic inverse is unsafe')
        # Reversal is exact and ordered: restore old cohorts before routing back,
        # then remove staged cohorts. No inverse touches application data.
        old=self.state.get('active_before')
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
            self.patch(plan['kind'],value,[{'op':'replace','path':'/spec','value':plan['before']}])
            if plan['kind']=='deployment':
                self.cluster.wait(plan['name'])
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
        self.save(rolled_back=True,recovery_required=False)

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
        self.save()
        plan=self.plan()
        # An already applied content-identical package is a verified no-op.
        try:
            proof=self.verify()
        except Exception:
            proof=None
        if proof is not None:
            self.resource_proof()
            self.save(completed=True,up_to_date=True,proof=proof,dag_was_paused=plan['idle']['paused'],drift_was_paused=plan['idle']['drift_paused'])
            return
        if plan['idle']['active'] or plan['idle']['running']:
            self.save(deferred=True,reason='active_training',retry_ci_run_id=os.environ.get('CI_RUN_ID'))
            return
        self.save(started=True,preflight_only=False,dag_was_paused=plan['idle']['paused'],
                  drift_was_paused=plan['idle']['drift_paused'],
                  active_before=plan['active'],target=plan['target'])
        try:
            self.pause(True)
            self.pause(True,drift=True)
            idle=self.cluster.remote(self.api(),IDLE)
            if idle['active'] or idle['running']:
                self.pause(plan['idle']['paused'])
                self.pause(plan['idle']['drift_paused'],drift=True)
                self.save(started=False,preflight_only=True,deferred=True,reason='training_started_before_fence',retry_ci_run_id=os.environ.get('CI_RUN_ID'))
                return
            self.start_guard()
            self.business()
            self.public_health()
            self.monitor_start()
            if plan['target'] in plan['changes']:
                self.change('deployment',plan['target'],plan['changes'][plan['target']])
                self.cluster.wait(plan['target'])
            self.health(plan['target'],'api')
            self.route('api-preview',plan['target']); self.endpoints('api-preview',plan['target'],2)
            self.business('api-preview')
            self.monitor_check()
            for role in ('tree','neural'):
                name=plan['targets'][role]
                if name in plan['changes']:
                    self.change('deployment',name,plan['changes'][name]); self.cluster.wait(name)
                self.health(name,role)
            self.airflow()
            self.monitor_check()
            self.peer(True)
            for service,app,count in [('traffic-training-tree',plan['targets']['tree'],1),('traffic-training-worker',plan['targets']['neural'],1),
                                      ('api-peers',plan['target'],2),('traffic-api-training',plan['target'],2),('traffic-api',plan['target'],2)]:
                self.route(service,app); self.endpoints(service,app,count)
            self.peer(False)
            proof=self.verify()
            # Allow kube-proxy/ingress endpoint propagation before draining.
            time.sleep(15)
            if plan['active']!=plan['target']:
                if plan['active']=='traffic-api':
                    self.drain_legacy(plan['active'])
                else:
                    self.drain_managed(plan['active'])
                self.wait_retired(plan['active'])
            # Retire the legacy idle watchers before allowing queued dispatch.
            for role in ('tree','neural'):
                old=plan['originals'][role]
                if old!=plan['targets'][role] and self.cluster.deployment(old)['spec'].get('replicas',0):
                    self.scale(old,0); self.wait_retired(old)
            self.business()
            self.monitor_stop()
            self.pause(plan['idle']['paused'])
            self.pause(plan['idle']['drift_paused'],drift=True)
            self.release_guard()
            self.resource_proof()
            self.save(completed=True,recovery_required=False,proof=proof)
        except Exception:
            self.save(recovery_required=True)
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
    args=parser.parse_args()
    op=RolesDelivery(Cluster(args.kubectl,args.context,args.namespace_uid,args.kubeconfig),args.directory,args.binding,
                     args.journal,commit=args.commit,kubeconfig=args.kubeconfig)
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
    except Exception as error:
        print(json.dumps({'delivery_failed':True,'error_type':type(error).__name__,
                          'reason':str(error) if isinstance(error,(ValueError,RuntimeError)) else 'Details omitted.',
                          'recovery_required':op.state.get('recovery_required',True)}))
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
