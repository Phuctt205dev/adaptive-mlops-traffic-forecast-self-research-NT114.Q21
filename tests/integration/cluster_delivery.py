"""Run the actual delivery engine on an isolated, GitHub-hosted kind cluster.

HTTP/Airflow APIs use explicit fixtures. Actual baseline/role images, import
loaders, real RF inference, PostgreSQL leases, MinIO, sync CLI and Kubernetes
rollouts/Service routing are exercised. No production kubeconfig is accepted.
"""
import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.cicd.deploy import Cluster,ready_pods
from scripts.cicd.roles_deploy import RolesDelivery
from scripts.cicd.roles_release import build,verify
from scripts.cicd.role_manifests import render,SLOTS
from scripts.cicd.bind_roles import bind
from scripts.cicd.recover_cd import confirm_roles
from scripts.cicd.diagnostics import collect

PG='docker.io/library/postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea'
MINIO='docker.io/pgsty/minio@sha256:b6bfe7239bfc83fb90d31612d9704d86039dd714f7904b3f1ad68f211e602372'
NAMESPACE='traffic-mlops';CONTEXT='kind-cd-e2e';COMMIT='e'*40


def command(*args,input=None,timeout=600):
    p=subprocess.run(list(args),input=input,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError('Fixture command failed: '+args[0]+'; '+p.stderr[-1500:])
    return p.stdout


def service(name,app,port=8000):
    return {'apiVersion':'v1','kind':'Service','metadata':{'name':name,'namespace':NAMESPACE},
            'spec':{'selector':{'app':app},'ports':[{'port':port,'targetPort':port}]}}


class Suite:
    def __init__(self,kubeconfig,release,fixtures,mode='migration'):
        if mode not in {'current','migration'}:raise ValueError('Unknown isolated suite')
        self.mode=mode;self.started=time.monotonic();self.case_started=self.started
        if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':
            raise RuntimeError('This fixture may run only on a GitHub-hosted disposable runner')
        context=command('kubectl','--kubeconfig',str(kubeconfig),'config','current-context').strip()
        config=json.loads(command('kubectl','--kubeconfig',str(kubeconfig),'config','view','--minify','-o','json'))
        if context!=CONTEXT or not config['clusters'][0]['cluster']['server'].startswith('https://127.0.0.1:'):
            raise RuntimeError('Refusing a non-disposable Kubernetes target')
        self.kubeconfig=Path(kubeconfig);self.temp=Path(tempfile.mkdtemp(prefix='cd-isolated-'))
        self.fixtures=Path(fixtures);self.bindings=json.loads((Path(release)/'delivery.json').read_text())
        self.cases=[];self.forwarders=[]
        command('kubectl','--kubeconfig',str(kubeconfig),'create','namespace',NAMESPACE)
        identity=json.loads(command('kubectl','--kubeconfig',str(kubeconfig),'get','namespace',NAMESPACE,'-o','json'))['metadata']['uid']
        self.cluster=Cluster('kubectl',CONTEXT,identity,str(kubeconfig))
        self.source=self.temp/'source'
        for name in ('backend','src','scripts','deployment','requirements'):
            shutil.copytree(ROOT/name,self.source/name,ignore=shutil.ignore_patterns('__pycache__'))
        (self.source/'docs').mkdir();shutil.copy2(ROOT/'docs/index.html',self.source/'docs/index.html')
        self.profile()
        self.progress('setup')

    def create(self,value):return self.cluster.call('create','-f','-','-o','json',value=value)

    def profile(self):
        for role in ('api','tree','neural'):
            p=self.source/'deployment/rke2/roles'/(role+'.json');v=json.loads(p.read_text());pod=v['spec']['template']['spec']
            raw=json.dumps(pod).replace('mlops-node-02','cd-e2e-worker').replace('mlops-node-03','cd-e2e-worker2')
            pod=json.loads(raw);v['spec']['template']['spec']=pod
            main=pod['containers'][0];main['command']=['python','-B','/fixtures/runtime_fixture.py','role']
            for env in main['env']:
                if env['name']=='CLOUD_TRAINING_ENABLED':env['value']='false'
                if env['name']=='KAGGLE_USERNAME':env['value']='ci-only'
            # Airflow logs remain an actual PVC in production; this fixture
            # does not require a storage provisioner or production log volume.
            for volume in pod['volumes']:
                if 'persistentVolumeClaim' in volume:
                    volume.pop('persistentVolumeClaim');volume['emptyDir']={}
            pod['volumes'].append({'name':'fixtures','configMap':{'name':'fixture-runtime'}})
            for item in [*pod['containers'],*pod.get('initContainers',[])]:
                item.setdefault('volumeMounts',[]).append({'name':'fixtures','mountPath':'/fixtures','readOnly':True})
                item.setdefault('env',[]).extend([{'name':'SSL_CERT_FILE','value':'/fixtures/server.crt'},
                                                 {'name':'AWS_REQUEST_CHECKSUM_CALCULATION','value':'when_required'}])
            p.write_text(json.dumps(v))
        p=self.source/'deployment/rke2/runtime-contract.json';c=json.loads(p.read_text())
        # Only kilobytes of synthetic data are restored in this test profile.
        c['capacity']['minimum_disk_bytes']=1024**3;p.write_text(json.dumps(c))

    def release(self,label,*,revision='one',worker_revision=None,bad_legacy=False,smoke=False,airflow_change=False):
        path=self.source/'deployment/rke2/roles/api.json';original=path.read_text();value=json.loads(original)
        env=value['spec']['template']['spec']['containers'][0]['env']
        env.append({'name':'CI_CONFIG_REVISION','value':revision})
        if smoke:env.append({'name':'FIXTURE_FAIL_SMOKE','value':'true'})
        path.write_text(json.dumps(value))
        migration=self.source/'deployment/rke2/migrations/role-runtime-v1.json';migration_original=migration.read_text()
        if bad_legacy:
            m=json.loads(migration_original);m['uploader_sha256']='0'*64;migration.write_text(json.dumps(m))
        dag=self.source/'deployment/rke2/runtime/traffic_region_training_cloud.py';dag_original=dag.read_text()
        if airflow_change:dag.write_text(dag_original+'\n# isolated changed Airflow source\n')
        worker_originals={}
        if worker_revision is not None:
            for role in ('tree','neural'):
                p=self.source/'deployment/rke2/roles'/(role+'.json');worker_originals[p]=p.read_text()
                v=json.loads(worker_originals[p]);v['spec']['template']['spec']['containers'][0]['env'].append(
                    {'name':'CI_WORKER_REVISION','value':worker_revision})
                p.write_text(json.dumps(v))
        out=self.temp/label
        try:build(self.source,out,COMMIT)
        finally:
            path.write_text(original);migration.write_text(migration_original);dag.write_text(dag_original)
            for p,original in worker_originals.items():p.write_text(original)
        binding_dir=self.temp/(label+'-bindings');binding_dir.mkdir()
        for role,v in self.bindings['images'].items():
            (binding_dir/('image-'+role+'.json')).write_text(json.dumps(v))
        bind(out,binding_dir,COMMIT)
        checked=self.temp/(label+'-checked');shutil.copytree(out,checked,ignore=shutil.ignore_patterns('delivery.json'))
        return checked,out/'delivery.json'

    def operator(self,release,label,migration=False):
        self.case_started=time.monotonic()
        self.progress(label)
        print(json.dumps({'isolated_case_started':label}),flush=True)
        return RolesDelivery(self.cluster,release[0],release[1],self.temp/(label+'.json'),commit=COMMIT,
                             kubeconfig=str(self.kubeconfig),migration='role-runtime-v1' if migration else None)

    def progress(self,current_case):
        # Persist before starting a case and after each completed case. A runner
        # timeout may prevent the final exception handler from being executed.
        # This distinct schema is evidence of progress, never acceptance.
        proof={'schema':'traffic-isolated-delivery-progress/v1','complete':False,
               'production_accessed':False,'current_case':current_case,'completed_cases':self.cases}
        out=ROOT/'results/cicd/kubernetes-delivery-progress.json'
        out.parent.mkdir(parents=True,exist_ok=True)
        temporary=out.with_suffix('.tmp');temporary.write_text(json.dumps(proof,indent=2),encoding='utf-8')
        temporary.replace(out)

    def simple(self,name,image,mode,port,*,env=None,init=None,volumes=None,mounts=None):
        pod={'restartPolicy':'Always','automountServiceAccountToken':False,
             'containers':[{'name':name,'image':image,'command':['python','-B','/fixtures/runtime_fixture.py',mode,'--port',str(port)],
                            'ports':[{'containerPort':port}],'env':env or [],
                            'envFrom':[{'configMapRef':{'name':'traffic-config'}},{'secretRef':{'name':'traffic-secrets'}}],
                            'volumeMounts':[{'name':'fixtures','mountPath':'/fixtures','readOnly':True},*(mounts or [])],
                            'readinessProbe':{'tcpSocket':{'port':port},'periodSeconds':2},
                            'resources':{'requests':{'cpu':'25m','memory':'128Mi'},'limits':{'memory':'1Gi'}}}],
             'volumes':[{'name':'fixtures','configMap':{'name':'fixture-runtime'}},*(volumes or [])]}
        if init:pod['initContainers']=init
        self.create({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':name,'namespace':NAMESPACE},
                     'spec':{'replicas':1,'selector':{'matchLabels':{'app':name}},'template':{'metadata':{'labels':{'app':name}},'spec':pod}}})

    def setup(self,release):
        print(json.dumps({'isolated_setup':'database_and_storage'}),flush=True)
        certificate=self.temp/'server.crt';key=self.temp/'server.key'
        command('openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(certificate),
                '-days','1','-subj','/CN=ingress.traffic-mlops.svc.cluster.local',
                '-addext','subjectAltName=DNS:ingress.traffic-mlops.svc.cluster.local')
        files={'runtime_fixture.py':(ROOT/'tests/integration/runtime_fixture.py').read_text(),
               'server.crt':certificate.read_text(),'server.key':key.read_text(),
               'expected.json':(self.fixtures/'expected.json').read_text()}
        binary={n:base64.b64encode((self.fixtures/n).read_bytes()).decode() for n in ('random_forest.joblib','tree-input.npy')}
        self.create({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'fixture-runtime','namespace':NAMESPACE},'data':files,'binaryData':binary})
        self.create({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'legacy-cleanup','namespace':NAMESPACE},
                     'data':{p.name:p.read_bytes().decode('utf-8') for p in (ROOT/'tests/fixtures/legacy_region_cleanup').iterdir() if p.suffix in {'.py','.json'}}})
        self.create({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'traffic-config','namespace':NAMESPACE},
                     'data':{'TRAFFIC_API_RUNTIME_BUCKET':'ci-runtime','S3_ENDPOINT_URL':'http://minio:9000','AIRFLOW_API_URL':'http://airflow-webserver:8080/api/v1',
                             'CLOUD_TRAINING_ENABLED':'false','AWS_EC2_METADATA_DISABLED':'true','TRAFFIC_API_ARTIFACT_SYNC_SECONDS':'5',
                             'SSL_CERT_FILE':'/fixtures/server.crt','AWS_REQUEST_CHECKSUM_CALCULATION':'when_required'}})
        secret={'DATABASE_URL':'postgresql+psycopg://ci:ci-only-password@postgres:5432/ci',
                'MINIO_ROOT_USER':'ci-only-user','MINIO_ROOT_PASSWORD':'ci-only-password',
                'AWS_ACCESS_KEY_ID':'ci-only-user','AWS_SECRET_ACCESS_KEY':'ci-only-password',
                'INTERNAL_TRAINING_TOKEN':'ci-only-internal','AUTH_SECRET_KEY':'ci-only-auth-key',
                'AIRFLOW_ADMIN_USERNAME':'ci-only','AIRFLOW_ADMIN_PASSWORD':'ci-only-password'}
        self.create({'apiVersion':'v1','kind':'Secret','metadata':{'name':'traffic-secrets','namespace':NAMESPACE},'stringData':secret})
        self.create({'apiVersion':'v1','kind':'Secret','metadata':{'name':'traffic-kaggle-credentials','namespace':NAMESPACE},
                     'stringData':{'kaggle-api-token':'ci-provider-disabled','api_token.txt':'ci-provider-disabled'}})
        for name,image,port,env,args in [
            ('postgres',PG,5432,{'POSTGRES_USER':'ci','POSTGRES_PASSWORD':'ci-only-password','POSTGRES_DB':'ci'},[]),
            ('minio-storage',MINIO,9000,{'MINIO_ROOT_USER':'ci-only-user','MINIO_ROOT_PASSWORD':'ci-only-password'},['server','/data'])]:
            self.create({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':name,'namespace':NAMESPACE},
                         'spec':{'replicas':1,'selector':{'matchLabels':{'app':name}},'template':{'metadata':{'labels':{'app':name}},
                           'spec':{'containers':[{'name':name,'image':image,'args':args,'env':[{'name':k,'value':v} for k,v in env.items()],
                                   'ports':[{'containerPort':port}],'readinessProbe':{'tcpSocket':{'port':port},'periodSeconds':2}}]}}}})
            self.create(service(name,name,port));self.cluster.wait(name)
        images=self.bindings['images'];sync_image=images['sync']['image'];api_image=images['api']['image']
        self.simple('minio',sync_image,'s3',9000);self.create(service('minio','minio',9000));self.cluster.wait('minio')
        self.simple('mlflow',sync_image,'health',5000);self.create(service('mlflow','mlflow',5000));self.cluster.wait('mlflow')
        seed={'apiVersion':'batch/v1','kind':'Job','metadata':{'name':'seed','namespace':NAMESPACE},'spec':{'backoffLimit':0,
              'template':{'spec':{'restartPolicy':'Never','containers':[{'name':'seed','image':api_image,
               'command':['python','-B','/fixtures/runtime_fixture.py','seed'],
               'envFrom':[{'configMapRef':{'name':'traffic-config'}},{'secretRef':{'name':'traffic-secrets'}}],
               'volumeMounts':[{'name':'fixtures','mountPath':'/fixtures'}]}],'volumes':[{'name':'fixtures','configMap':{'name':'fixture-runtime'}}]}}}}
        self.create(seed);self.cluster.call('wait','--for=condition=complete','job/seed','--timeout=180s',json_output=False,timeout=190)
        self.airflow(release)
        print(json.dumps({'isolated_setup':self.mode+'_cohorts'}),flush=True)
        if self.mode=='current':
            for role,slots in SLOTS.items():
                for index,slot in enumerate(slots):
                    self.create(render(role,slot,images,directory=release[0],inactive=index!=0))
            # Submit independent cohorts together, then require every cohort
            # Ready before any test. Keep the actual readiness/grace settings.
            for slots in SLOTS.values():
                self.cluster.wait(slots[0])
            routes=[('traffic-api','api'),('traffic-api-training','api'),('api-peers','api'),
                    ('api-preview','api'),('traffic-training-tree','train-tree'),('traffic-training-worker','train-neural')]
            self.routes(routes,sync_image)
            return
        baseline=json.loads((release[0]/'release.json').read_text())['baseline']['legacy_application_image']
        for role,legacy in [('api','traffic-api'),('tree','traffic-training-tree'),('neural','traffic-training-worker')]:
            first=SLOTS[role][0];value=render(role,first,images,directory=release[0])
            spec=value['spec'];value['metadata']['name']=legacy;spec['selector']['matchLabels']['app']=legacy
            spec['template']['metadata']={'labels':{'app':legacy,'traffic-region-cleanup-peer':'true'}}
            pod=spec['template']['spec'];pod['containers'][0]['image']=baseline
            if role=='api':
                pod['containers'][0].pop('lifecycle',None)
                pod['containers'][0]['readinessProbe']['httpGet']['path']='/api/v1/health/ready'
                pod['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution'][0]['labelSelector']['matchLabels']['app']=legacy
                sync=pod['initContainers'].pop();sync.pop('restartPolicy',None);sync['name']='artifact-cache-sync';sync['image']=baseline
                sync['command']=['python','-B','/opt/traffic-release/cache_sync.py']
                sync['volumeMounts'].append({'name':'legacy-cleanup','mountPath':'/opt/region-cleanup','readOnly':True})
                pod['volumes'].append({'name':'legacy-cleanup','configMap':{'name':'legacy-cleanup'}});pod['containers'].append(sync)
            self.create(value)
            for slot in SLOTS[role]:self.create(render(role,slot,images,directory=release[0],inactive=True))
            self.cluster.wait(legacy)
            print(json.dumps({'isolated_legacy_ready':role}),flush=True)
        self.routes([('traffic-api','traffic-api'),('traffic-api-training','traffic-api'),('api-peers','traffic-api'),
                     ('api-preview','api'),('traffic-training-tree','traffic-training-tree'),('traffic-training-worker','traffic-training-worker')],sync_image)

    def routes(self,routes,sync_image):
        for name,app in routes:
            self.create(service(name,app))
        self.simple('ingress',sync_image,'proxy',8443);self.create(service('ingress','ingress',8443));self.cluster.wait('ingress')
        os.environ['PUBLIC_API_BASE_URL']='https://ingress.traffic-mlops.svc.cluster.local:8443/api/v1'
        os.environ['CI_RUN_ID']=os.environ['GITHUB_RUN_ID']

    def airflow(self,release):
        suffix='\n# initial fixture\n' if self.mode=='migration' else ''
        files={'dag.py':(release[0]/'airflow/dag.py').read_text()+suffix,
               'airflow_control.py':(release[0]/'airflow/airflow_control.py').read_text()+suffix,
               'legacy.py':(release[0]/'airflow/legacy/traffic_region_training_worker.py').read_text()+suffix}
        self.create({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'initial-airflow','namespace':NAMESPACE},'data':files})
        image=json.loads((release[0]/'release.json').read_text())['baseline']['airflow_image']
        volume={'name':'dag-source','configMap':{'name':'initial-airflow','items':[{'key':'dag.py','path':'dag.py'},
                    {'key':'airflow_control.py','path':'airflow_control.py'},{'key':'legacy.py','path':'legacy/traffic_region_training_worker.py'}]}}
        mounts=[{'name':'dag-source','mountPath':'/opt/traffic-cloud'},
                {'name':'dag-source','mountPath':'/opt/airflow/dags/traffic_region_training.py','subPath':'dag.py'}]
        env=[{'name':'AIRFLOW__CORE__EXECUTOR','value':'LocalExecutor'},
             {'name':'AIRFLOW__DATABASE__SQL_ALCHEMY_CONN','value':'postgresql+psycopg2://ci:ci-only-password@postgres:5432/ci_airflow'}]
        for name in ('airflow-webserver','airflow-scheduler'):
            self.simple(name,image,'airflow',8080,env=env,volumes=[volume],mounts=mounts)
            self.create(service(name,name,8080));self.cluster.wait(name)

    def record(self,name,op,**facts):
        api=self.cluster.call('get','service','traffic-api','-o','json')['spec']['selector']['app']
        source="from sqlalchemy import text;from backend.app.db.session import SessionLocal;import json\nwith SessionLocal() as db:\n n=db.execute(text(\"SELECT count(*) FROM training_runs r JOIN training_executions e ON e.training_run_id=r.id WHERE r.split_policy='ci-only-queue' AND r.status='queued' AND e.state='queued'\")).scalar_one()\nprint(json.dumps({'queued_database_rows':n}))"
        queue=self.cluster.remote(self.cluster.deployment(api),source)
        assert queue['queued_database_rows']==1,'Controller changed synthetic queued training metadata'
        self.cases.append({'case':name,**{k:op.state.get(k) for k in ('completed','up_to_date','deferred','rolled_back','recovery_required',
                           'guard_held','failed_phase','execution_failure','probe_failure','availability','flush_proofs')},
                           **queue,**facts,'duration_seconds':round(time.monotonic()-self.case_started,3)})
        self.progress(None)
        print(json.dumps({'isolated_case_passed':name}),flush=True)

    def assert_recovered(self,op):
        assert op.state['rolled_back'] and not op.state['guard_held'] and not op.state['recovery_required']
        confirm_roles(self.cluster,op.state)

    def long_request(self,op):
        pod=self.cluster.pod(self.active_api())
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        log=(self.temp/'port-forward.log').open('w')
        process=subprocess.Popen(['kubectl','--kubeconfig',str(self.kubeconfig),'-n',NAMESPACE,'port-forward',
                                  'pod/'+pod,str(port)+':8000','--address=127.0.0.1'],stdout=log,stderr=log)
        self.forwarders.append(process);result={};stop=threading.Event()
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=1):break
            except OSError:time.sleep(.2)
        else:raise RuntimeError('Fixture port forward was not ready')
        def request():
            deadline=time.monotonic()+1500
            while op.state.get('phase')!='promote' and not stop.is_set() and time.monotonic()<deadline:stop.wait(.1)
            if stop.is_set():return
            try:
                with urlopen('http://127.0.0.1:'+str(port)+'/api/v1/_fixture/slow?seconds=45',timeout=90) as r:
                    result.update(status=r.status,body=json.load(r))
            except Exception as error:result['error_type']=type(error).__name__
        thread=threading.Thread(target=request,daemon=True);thread.start()
        try:
            op.apply();thread.join(95)
            assert result.get('status')==200 and result.get('body',{}).get('request_completed'),result
            op.save(long_request_completed_before_retirement=True)
        finally:
            stop.set();process.terminate();process.wait(timeout=5);log.close()

    def busy(self,value):
        api=self.cluster.deployment(self.cluster.call('get','service','traffic-api','-o','json')['spec']['selector']['app'])
        code="import requests,json;r=requests.post('http://airflow-webserver:8080/_fixture/busy',json={'busy':VALUE},timeout=5);r.raise_for_status();print(json.dumps(r.json()))"
        self.cluster.remote(api,code.replace('VALUE',repr(value)))

    def active_api(self):
        app=self.cluster.call('get','service','traffic-api','-o','json')['spec']['selector']['app']
        return self.cluster.deployment(app)

    def api_snapshot(self):
        deployment=self.active_api()
        pods=ready_pods(deployment,self.cluster.call('get','pods','-o','json')['items'])
        return {'deployment':deployment['metadata']['name'],'spec':deployment['spec'],
                'pods':sorted((p['metadata']['uid'],tuple((s['name'],s.get('restartCount',0))
                         for s in p['status']['containerStatuses'])) for p in pods)}

    def run(self):
        base=self.release('base');self.setup(base)
        if self.mode=='current':
            # Start from the managed topology directly, using real role images,
            # native sync sidecars and restored objects; no migration shortcut.
            changed=self.release('managed-update',revision='warm')
            # One real rollout covers manifest-only updates and long-request
            # draining together. All four immutable image bindings stay fixed.
            original=json.loads(base[1].read_text());desired=json.loads(changed[1].read_text())
            assert original['images']==desired['images'] and original['deployments']['api']!=desired['deployments']['api']
            update=self.operator(changed,'managed-update')
            self.long_request(update);update.verify()
            assert update.state['completed'] and update.state['flush_proofs']
            self.record('managed_update_with_long_request',update,manifest_only_update=True)
            before=self.api_snapshot()
            workers=self.operator(self.release('worker-update',revision='warm',worker_revision='two'),'worker-update')
            workers.apply();workers.verify()
            assert workers.state['completed'] and self.api_snapshot()==before
            assert {'tree-next','neural-next'} <= set(workers.state['patched'])
            self.record('worker_only_update_preserves_api',workers)
            # Keep the worker desired manifests steady during subsequent API cases.
            for role in ('tree','neural'):
                p=self.source/'deployment/rke2/roles'/(role+'.json');v=json.loads(p.read_text())
                v['spec']['template']['spec']['containers'][0]['env'].append({'name':'CI_WORKER_REVISION','value':'two'})
                p.write_text(json.dumps(v))
            steady=self.release('steady',revision='warm')
            return self.regular_cases(steady,update)
        normal=self.operator(base,'regular-before-migration')
        try:normal.apply()
        except ValueError:pass
        else:raise AssertionError('Legacy runtime entered the regular release path')
        assert not normal.state['started'] and not normal.state['patched'];self.record('migration_required',normal)
        bad=self.operator(self.release('bad-baseline',bad_legacy=True),'bad-baseline',True)
        try:bad.apply()
        except RuntimeError:pass
        else:raise AssertionError('Wrong legacy source hash passed preflight')
        assert not bad.state['started'] and not bad.state['patched']
        assert bad.state['execution_failure']['container']=='artifact-cache-sync'
        self.record('legacy_preflight_failure_before_mutation',bad)
        migration=self.operator(base,'migration',True);self.long_request(migration);migration.verify()
        assert migration.state['completed'] and migration.state['legacy_drain']['cache_flush_confirmed'];self.record('actual_migration',migration)
        # The current suite independently covers ordinary rollback/defer/no-op.
        # Do not repeat those same cycles after the legacy migration.
        return self.proof(migration)

    def regular_cases(self,base,long_operation):
        self.busy(True)
        try:
            busy=self.operator(self.release('busy',revision='two'),'busy');busy.apply()
            assert busy.state['deferred'] and not busy.state['started'] and busy.state['patched']==[] and not busy.state.get('guard_job')
            self.record('active_training_deferred_without_mutation',busy)
        finally:self.busy(False)
        smoke=self.operator(self.release('smoke',revision='two',smoke=True),'smoke')
        try:smoke.apply()
        except RuntimeError:pass
        else:raise AssertionError('Failed business probe was accepted')
        self.assert_recovered(smoke);assert smoke.state['probe_failure']['http_status']==503;self.record('preview_smoke_rollback',smoke)
        self.retirement_failure()
        # The failure cases must have restored the desired steady release.
        # Prove that rollback converged without another successful rollout.
        before=self.api_snapshot()
        final=self.operator(base,'final-noop');final.apply();final.verify()
        assert final.state['up_to_date'] and final.state['patched']==[] and not final.state.get('guard_job')
        assert self.api_snapshot()==before
        self.record('post_update_noop',final)
        return self.proof(long_operation)

    def proof(self,long_operation):
        queue=self.cluster.remote(self.active_api(),
            "import json,requests;print(json.dumps(requests.get('http://airflow-webserver:8080/_fixture/state',timeout=5).json()))")
        assert queue['queued']==1 and queue['provider_calls']==0 and not any(queue['paused'].values())
        return {'schema':'traffic-isolated-delivery-proof/v3','suite':self.mode,'source_commit':os.environ['GITHUB_SHA'],
                'ci_run_id':os.environ['GITHUB_RUN_ID'],'ci_attempt':os.environ['GITHUB_RUN_ATTEMPT'],
                'release_id':self.bindings['release_id'],'images':self.bindings['images'],
                'duration_seconds':round(time.monotonic()-self.started,3),'kubernetes_context':CONTEXT,
                'production_accessed':False,'real_baseline_and_role_images':True,'business_http_is_fixture':True,
                'real_rf_inference':True,'real_database_and_object_storage':True,'provider_called':False,
                'long_request_completed':long_operation.state['long_request_completed_before_retirement'],
                'queue_preserved':True,'cases':self.cases}

    def retirement_failure(self):
        # The fixture proxy rejects one synthetic object only. Genuine boto3
        # upload fails; no monkeypatch of the engine, sync CLI or rollback.
        api=self.active_api();pod=self.cluster.pod(api)
        control="import requests,json;r=requests.post('http://minio:9000/_fixture/deny',json={'deny':VALUE},timeout=5);r.raise_for_status();print(json.dumps({'fixture_s3_control':True}))"
        self.cluster.remote(api,control.replace('VALUE','True'))
        self.cluster.call('exec',pod,'-c','api','--','python','-B','-c',"from pathlib import Path;Path('/app/models/unacknowledged.txt').write_text('synthetic only')",json_output=False)
        op=self.operator(self.release('retire-failure',revision='two',airflow_change=True),'retire-failure')
        try:
            try:op.apply()
            except RuntimeError:pass
            else:raise AssertionError('Unacknowledged upload was accepted')
            self.assert_recovered(op)
            assert op.state['failed_phase']=='retire' and op.state['execution_failure']['error_type']=='S3UploadFailedError'
            assert any(p['name']=='airflow-scheduler' for p in op.inverses)
            self.record('post_promote_flush_failure_and_server_default_rollback',op)
        finally:self.cluster.remote(self.active_api(),control.replace('VALUE','False'))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--kubeconfig',required=True);p.add_argument('--release',required=True);p.add_argument('--fixtures',required=True)
    p.add_argument('--suite',choices=['current','migration'],default='migration')
    args=p.parse_args();suite=Suite(args.kubeconfig,args.release,args.fixtures,args.suite)
    try:
        proof=suite.run();out=ROOT/'results/cicd/kubernetes-delivery-proof.json';out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(proof,indent=2));print(json.dumps({'isolated_cases_passed':len(proof['cases']),'production_accessed':False}))
    except Exception as error:
        # No raw logs/specs/Secrets or generated TLS key are uploaded.
        receipts=[]
        for p in suite.temp.glob('*.json'):
            value=json.loads(p.read_text())
            if value.get('schema')=='traffic-role-delivery/v2':
                receipts.append({'case':p.stem,**{k:value.get(k) for k in ('phase','failed_phase','error_type','execution_failure','probe_failure','rolled_back','recovery_required')}})
        proof={'isolated_delivery_failed':True,'failure_type':type(error).__name__,'completed_cases':[c['case'] for c in suite.cases],
               'receipts':receipts,'pod_facts':collect(suite.cluster,['api','api-next','train-tree','train-neural','airflow-scheduler','airflow-webserver','seed','postgres','minio','minio-storage']),
               'production_accessed':False}
        out=ROOT/'results/cicd/kubernetes-delivery-proof.json';out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(proof,indent=2))
        print(json.dumps({'isolated_delivery_failed':True,'failure_type':type(error).__name__,'completed_cases':proof['completed_cases']}))
        raise
    finally:
        for process in suite.forwarders:
            if process.poll() is None:process.terminate();process.wait(timeout=5)
