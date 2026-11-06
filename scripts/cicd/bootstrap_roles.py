"""One-time operator bootstrap: create empty cohorts and narrow runner access.

Plan uses server dry-runs. Apply creates only missing zero-replica deployments,
preview/peer Services and a PDB; existing serving workloads are never replaced.
"""
import argparse
import copy
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from scripts.cicd.deploy import Cluster
from scripts.cicd.roles_release import verify
from scripts.cicd.roles_deploy import validate_delivery, SERVICES
from scripts.cicd.role_blueprints import deployment, headless, preview

COHORTS={'api':('api','api-next'),'tree':('train-tree','tree-next'),'neural':('train-neural','neural-next')}


def bootstrap(cluster, directory, binding, *, apply=False):
    release=verify(directory)
    images=validate_delivery(release,json.loads(Path(binding).read_text(encoding='utf-8-sig')))
    if cluster.call('get','namespace','traffic-mlops','-o','json')['metadata']['uid']!=cluster.namespace_uid:
        raise RuntimeError('Wrong namespace')
    resources=[]
    for role,names in COHORTS.items():
        source=cluster.deployment(release['baseline']['roles'][role]['legacy'])
        for name in names:
            resources.append(deployment(source,name,role,images,images[role]['source_id']))
    active=cluster.call('get','service','traffic-api','-o','json')['spec']['selector']['app']
    resources += [headless('api-peers',active),preview(),
                  {'apiVersion':'policy/v1','kind':'PodDisruptionBudget','metadata':{'name':'api-availability','namespace':'traffic-mlops'},
                   'spec':{'minAvailable':1,'selector':{'matchLabels':{'traffic.mlops/role':'api'}}}}]
    created=[]
    for value in resources:
        kind,name=value['kind'],value['metadata']['name']
        old=cluster.call('get',kind,name,'--ignore-not-found','-o','json')
        if old:
            continue  # Never mutate active resources as part of bootstrap.
        args=['create','-f','-','-o','json']
        if not apply:
            args.append('--dry-run=server')
        cluster.call(*args,value=value)
        created.append(name)
    role=cluster.call('get','role','traffic-delivery','-o','json')
    rules=copy.deepcopy(role['rules'])
    extra=[{'apiGroups':['apps'],'resources':['deployments'],'resourceNames':[name for group in COHORTS.values() for name in group],'verbs':['patch']},
           {'apiGroups':[''],'resources':['services'],'resourceNames':list(SERVICES),'verbs':['get','patch']},
           {'apiGroups':[''],'resources':['endpoints'],'resourceNames':list(SERVICES),'verbs':['get']},
           {'apiGroups':[''],'resources':['configmaps'],'verbs':['get','create']}]
    for rule in extra:
        if rule not in rules:
            rules.append(rule)
    if rules!=role['rules']:
        value={'apiVersion':role['apiVersion'],'kind':'Role',
               'metadata':{'name':role['metadata']['name'],'namespace':'traffic-mlops','resourceVersion':role['metadata']['resourceVersion']},'rules':rules}
        args=['replace','-f','-','-o','json']
        if not apply:
            args.append('--dry-run=server')
        cluster.call(*args,value=value)
    return {'applied':apply,'empty_resources':created,'serving_workloads_unchanged':True,'secret_read_granted':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['Plan','Apply'])
    for name in ('directory','binding','kubectl','kubeconfig','context','namespace-uid'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    try:
        cluster=Cluster(args.kubectl,args.context,args.namespace_uid,args.kubeconfig)
        print(json.dumps(bootstrap(cluster,args.directory,args.binding,apply=args.action=='Apply')))
    except Exception as error:
        print(json.dumps({'bootstrap_failed':True,'error_type':type(error).__name__,'details_omitted':True}))
        raise SystemExit(1)
