"""Pinned legacy adapter. The regular delivery engine never guesses layouts."""
import json
from pathlib import Path

from scripts.cicd.deploy import container,ready_pods

ID='role-runtime-v1'


class Migration:
    def __init__(self,operator):
        self.op=operator
        self.plan=json.loads((operator.directory/'deploy/migrations/role-runtime-v1.json').read_bytes())
        if (self.plan.get('schema')!='traffic-runtime-migration/v1' or self.plan.get('id')!=ID
                or self.plan.get('sync_contract')!='acknowledged-files-v1'
                or self.plan.get('from_image')!=operator.release['baseline']['legacy_application_image']
                or self.plan.get('uploader_path')!='/opt/traffic-release/sidecar/region_cleanup_uploader.py'
                or self.plan.get('loader_path')!='/opt/traffic-release/cache_sync.py'):
            raise ValueError('Unsupported legacy migration contract')

    def legacy(self,name,role):
        return name==self.plan['roles'][role]

    def source(self,action):
        if action not in {'preflight','flush','inventory'}:raise ValueError('Unsupported migration operation')
        helper=(self.op.directory/'roles/sync/backend/app/services/artifact_sync_control.py').read_text(encoding='utf-8')
        path=self.plan['uploader_path'];expected=self.plan['uploader_sha256']
        code="""import hashlib,importlib.util,json,sys
from pathlib import Path
p=Path(PATH)
if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=EXPECTED:
 raise RuntimeError('Legacy uploader identity differs')
loader=Path(LOADER)
if not loader.is_file() or hashlib.sha256(loader.read_bytes()).hexdigest()!=LOADER_HASH:
 raise RuntimeError('Legacy source loader identity differs')
s=importlib.util.spec_from_file_location('legacy_migration_loader',str(loader))
entry=importlib.util.module_from_spec(s);s.loader.exec_module(entry)
m=entry.load_uploader()
if not all(callable(getattr(m,name,None)) for name in ('UploadState','upload_cycle','file_fingerprint')):
 raise RuntimeError('Legacy uploader capabilities differ')
n={'__name__':'migration_sync_control'}
exec(HELPER,n)
"""
        code=code.replace('LOADER_HASH',repr(self.plan['loader_sha256'])).replace('LOADER',repr(self.plan['loader_path']))
        code=code.replace('PATH',repr(path)).replace('EXPECTED',repr(expected)).replace('HELPER',repr(helper))
        if action=='preflight':
            code+="files=n['inventory']('/cache',m);print(json.dumps({'legacy_uploader_verified':True,'inventory_files':len(files)}))"
        elif action=='flush':
            code+="print(json.dumps(n['flush']('/cache',module=m)))"
        else:
            cache=(self.op.directory/'roles/sync/backend/app/services/restore_cache.py').read_text(encoding='utf-8')
            code+=("import os\nr={'__name__':'migration_restore_preflight'}\nexec("+repr(cache)+",r)\n"
                   "print(json.dumps(r['inventory_report'](m.get_upload_client(),os.environ['TRAFFIC_API_RUNTIME_BUCKET'])))")
        return code

    def preflight(self,originals):
        for role,name in originals.items():
            if self.legacy(name,role):
                value=self.op.cluster.deployment(name)
                if container(value)[1]['image']!=self.plan['from_image']:
                    raise RuntimeError('Legacy image differs from the reviewed migration baseline')
        api=originals['api']
        if not self.legacy(api,'api'):return
        value=self.op.cluster.deployment(api)
        pods=ready_pods(value,self.op.cluster.call('get','pods','-o','json')['items'])
        if len(pods)!=2:raise RuntimeError('Legacy API capacity is not fully ready')
        for pod in pods:
            sidecar=next((c for c in pod['spec']['containers'] if c['name']==self.plan['api_sidecar']),None)
            if sidecar is None or sidecar['image']!=self.plan['from_image']:
                raise RuntimeError('Legacy sidecar differs from the reviewed baseline')
            proof=self.op.execute(pod['metadata']['name'],sidecar['name'],self.source('preflight'))
            if proof.get('legacy_uploader_verified') is not True:
                raise RuntimeError('Legacy uploader preflight failed')

    def retire_api(self,name):
        op=self.op;value=op.cluster.deployment(name)
        pods=ready_pods(value,op.cluster.call('get','pods','-o','json')['items'])
        if len(pods)!=value['spec']['replicas']:
            raise RuntimeError('Legacy cache state is not fully observable; retirement stopped')
        import time
        source="import json;from pathlib import Path;rows=[line.split() for file in ('/proc/net/tcp','/proc/net/tcp6') if Path(file).exists() for line in Path(file).read_text().splitlines()[1:]];print(json.dumps({'connections':sum(r[1].endswith(':1F40') and r[3]=='01' for r in rows)}))"
        for pod in pods:
            deadline=time.monotonic()+840;quiet=None
            while time.monotonic()<deadline:
                count=op.execute(pod['metadata']['name'],container(value)[1]['name'],source)['connections']
                quiet=(quiet or time.monotonic()) if count==0 else None
                if quiet is not None and time.monotonic()-quiet>=20:break
                time.sleep(2)
            else:raise RuntimeError('Legacy connections did not drain; retirement stopped')
            proof=op.execute(pod['metadata']['name'],self.plan['api_sidecar'],self.source('flush'),timeout=650)
            op.require_flush(proof,name)
        op.save(legacy_drain={'connections_quiet_seconds':20,'cache_flush_confirmed':True,'migration':ID})
        op.scale(name,0)
