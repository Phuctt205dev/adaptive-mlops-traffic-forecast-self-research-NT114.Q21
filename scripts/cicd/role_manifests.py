"""Render reviewed Git manifests. Cluster state is never a template source."""
import json
from pathlib import Path
import re

from scripts.cicd.release import canonical,digest,DIGEST_IMAGE

ROOT=Path(__file__).resolve().parents[2]/'deployment/rke2'
CONTRACT='traffic-runtime-contract/v1'
SLOTS={'api':('api','api-next'),'tree':('train-tree','tree-next'),'neural':('train-neural','neural-next')}
ANNOTATION='traffic.mlops/deployment-id'


def contract(directory=None):
    path=Path(directory)/'deploy/runtime-contract.json' if directory else ROOT/'runtime-contract.json'
    value=json.loads(path.read_bytes())
    if value.get('schema')!=CONTRACT or set(value.get('roles',{}))!=set(SLOTS):
        raise ValueError('Unsupported deployment contract')
    for role,slots in SLOTS.items():
        if value['roles'][role]['slots']!=list(slots) or value['roles'][role]['replicas']!=(2 if role=='api' else 1):
            raise ValueError('Unreviewed role topology')
    if value['sync_cli']!=['python','-B','-m','backend.app.services.artifact_sync_control']:
        raise ValueError('Unsupported artifact sync protocol')
    minimum=value.get('capacity',{}).get('minimum_disk_bytes')
    if type(minimum) is not int or minimum<1024**3:raise ValueError('Invalid staging disk reserve')
    return value


def render(role,slot,images,*,directory=None,inactive=False):
    settings=contract(directory)
    if role not in SLOTS or slot not in SLOTS[role]:
        raise ValueError('Unexpected role slot')
    path=Path(directory)/('deploy/'+role+'.json') if directory else ROOT/'roles'/(role+'.json')
    template=json.loads(path.read_bytes())
    tokens={'${slot}':slot,'${source_id}':images[role]['source_id']}
    for name in ('api','tree','neural','sync'):
        image=images[name]['image']
        if not DIGEST_IMAGE.fullmatch(image):raise ValueError('Role image must be immutable')
        tokens['${image.'+name+'}']=image
    def expand(value):
        if isinstance(value,dict):return {k:expand(v) for k,v in value.items()}
        if isinstance(value,list):return [expand(v) for v in value]
        if isinstance(value,str) and '${' in value:
            if value not in tokens:raise ValueError('Unknown manifest parameter')
            return tokens[value]
        return value
    value=expand(template)
    if (value.get('kind')!='Deployment' or value.get('apiVersion')!='apps/v1'
            or value.get('metadata')!={'name':slot,'namespace':'traffic-mlops'}
            or value['spec']['replicas']!=settings['roles'][role]['replicas']
            or value['spec']['selector']!={'matchLabels':{'app':slot}}):
        raise ValueError('Manifest resource ownership differs')
    pod=value['spec']['template']['spec']
    if pod.get('automountServiceAccountToken') is not False:
        raise ValueError('Application service account token must stay disabled')
    for item in pod.get('volumes',[]):
        if 'hostPath' in item:raise ValueError('Host mounts are not part of the runtime contract')
    for item in [*pod['containers'],*pod.get('initContainers',[])]:
        if item.get('securityContext',{}).get('privileged'):
            raise ValueError('Privileged runtime is unsupported')
        if item.get('image') not in {v['image'] for v in images.values()}:
            raise ValueError('Manifest has an unbound image')
        for env in item.get('env',[]):
            if 'value' in env and re.search(r'(PASSWORD|SECRET|ACCESS_KEY|DATABASE_URL|TOKEN)$',env['name']):
                raise ValueError('Manifest contains a literal credential')
    # Slot names affect placement/routing, not the identity of desired content.
    logical=expand(template)
    logical['metadata']['name']='role-slot'
    logical['spec']['selector']['matchLabels']['app']='role-slot'
    logical['spec']['template']['metadata']['labels']['app']='role-slot'
    for entry in logical['spec']['template']['spec'].get('affinity',{}).get('podAntiAffinity',{}).get('requiredDuringSchedulingIgnoredDuringExecution',[]):
        if entry.get('labelSelector',{}).get('matchLabels',{}).get('app')==slot:
            entry['labelSelector']['matchLabels']['app']='role-slot'
    protocol={'schema':settings['schema'],'role':settings['roles'][role]}
    if role=='api':protocol.update(sync_cli=settings['sync_cli'],lifecycle_prefix=settings['lifecycle_prefix'])
    identifier=digest(canonical({'manifest':logical,'contract':protocol}))
    value['spec']['template']['metadata'].setdefault('annotations',{}).update({ANNOTATION:identifier,'traffic.mlops/runtime-contract':CONTRACT})
    if inactive:value['spec']['replicas']=0
    return value


def deployment_id(value):
    return value['spec']['template']['metadata'].get('annotations',{}).get(ANNOTATION)
