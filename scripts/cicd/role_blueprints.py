"""Build role templates from reviewed live references, without Secret reads."""
import copy
import re

RUNTIME = ['python', '-B', '/app/deployment/rke2/runtime/role_runtime.py']
OLD_RUNTIME = ['python', '-B', '/opt/traffic-cloud/cloud_training_runtime.py']
OLD_MOUNTS = {'/opt/traffic-cloud', '/opt/traffic-runtime', '/opt/traffic-quality', '/opt/traffic-shap',
              '/opt/traffic-shap-deps', '/opt/traffic-training', '/opt/region-cleanup', '/opt/traffic-kaggle-sdk'}
REMOVED_ENV = {'KAGGLE_PYTHON', 'TRAFFIC_CLOUD_MODE', 'TRAFFIC_SHAP_MODE', 'TRAFFIC_TRAINING_MODE'}


def reference_only(pod):
    for container in [*pod.get('containers', []), *pod.get('initContainers', [])]:
        for value in container.get('env', []):
            if 'value' in value and re.search(r'(PASSWORD|TOKEN|SECRET|ACCESS_KEY|DATABASE_URL)$', value['name']):
                raise ValueError('A literal credential cannot enter a role blueprint')


def clean_container(container):
    result = copy.deepcopy(container)
    result['volumeMounts'] = [v for v in result.get('volumeMounts', []) if v['mountPath'] not in OLD_MOUNTS]
    result['env'] = [e for e in result.get('env', []) if e['name'] not in REMOVED_ENV]
    return result


def main_container(pod):
    found = [c for c in pod['containers'] if any(p.get('containerPort') == 8000 for p in c.get('ports', []))]
    if len(found) != 1:
        raise ValueError('Expected one HTTP role container')
    return found[0]


def deployment(source, name, role, images, source_id):
    allowed = {'api':{'api','api-next'},'tree':{'train-tree','tree-next'},'neural':{'train-neural','neural-next'}}
    if role not in allowed or name not in allowed[role]:
        raise ValueError('Unexpected role deployment')
    pod = copy.deepcopy(source['spec']['template']['spec'])
    reference_only(pod)
    original = main_container(pod)
    if original.get('command') not in (OLD_RUNTIME, RUNTIME):
        raise ValueError('Unknown source entrypoint')
    main = clean_container(original)
    main.update(name='api' if role == 'api' else 'worker', image=images[role]['image'], command=RUNTIME)
    main.pop('args', None)
    prefix = '/api/v1/health' if role == 'api' else '/health'
    main['startupProbe'] = {'httpGet':{'path':prefix+'/live','port':8000},'periodSeconds':5,'timeoutSeconds':3,'failureThreshold':60}
    main['readinessProbe'] = {'httpGet':{'path':prefix+('/serving-ready' if role=='api' else '/ready'),'port':8000},
                              'periodSeconds':5,'timeoutSeconds':5,'failureThreshold':3}
    main['livenessProbe'] = {'httpGet':{'path':prefix+'/live','port':8000},'periodSeconds':20,'timeoutSeconds':3,'failureThreshold':3}
    pod['terminationGracePeriodSeconds'] = 900
    init = []
    if role == 'api':
        main['lifecycle'] = {'preStop':{'exec':{'command':['python','-B','-m','backend.app.services.lifecycle']}}}
        sidecars = [c for c in [*pod['containers'],*pod.get('initContainers',[])]
                    if c['name'] in {'artifact-cache-sync','artifact-sync'}]
        if len(sidecars) != 1:
            raise ValueError('Expected exactly one artifact sync contract')
        sync = clean_container(sidecars[0])
        sync.update(name='artifact-sync', image=images['sync']['image'], command=RUNTIME, restartPolicy='Always')
        sync.pop('args', None)
        sync.pop('lifecycle', None)
        init = [copy.deepcopy(c) for c in pod.get('initContainers',[]) if c['name'].startswith('wait-')]
        restore = {'name':'restore-cache','image':images['sync']['image'],'imagePullPolicy':'IfNotPresent',
                   'command':['python','-B','-m','backend.app.services.restore_cache','restore'],
                   'envFrom':copy.deepcopy(sync.get('envFrom',[])), 'env':copy.deepcopy(sync.get('env',[])),
                   'volumeMounts':copy.deepcopy(sync['volumeMounts']),
                   'resources':{'requests':{'cpu':'25m','memory':'64Mi'},'limits':{'cpu':'500m','memory':'256Mi'}}}
        init += [restore, sync]
    else:
        init = [{'name':'prepare-cache','image':images['sync']['image'],'imagePullPolicy':'IfNotPresent',
                 'command':['python','-B','-m','backend.app.services.restore_cache','prepare'],
                 'volumeMounts':[{'name':'worker-cache','mountPath':'/cache'}],
                 'resources':{'requests':{'cpu':'10m','memory':'32Mi'},'limits':{'cpu':'100m','memory':'128Mi'}}}]
    pod['containers'] = [main]
    pod['initContainers'] = init
    needed = {v['name'] for c in [main,*init] for v in c.get('volumeMounts',[])}
    pod['volumes'] = [v for v in pod.get('volumes',[]) if v['name'] in needed]
    if needed != {v['name'] for v in pod['volumes']}:
        raise ValueError('A role cache or credential mount is missing')
    labels = copy.deepcopy(source['spec']['template']['metadata'].get('labels',{}))
    old_app = labels.get('app')
    labels.pop('traffic-region-cleanup-peer',None)
    labels.update(app=name, **{'traffic.mlops/role':role,'traffic.mlops/managed-by':'cd'})
    for group in pod.get('affinity',{}).get('podAntiAffinity',{}).get('requiredDuringSchedulingIgnoredDuringExecution',[]):
        selector = group.get('labelSelector',{}).get('matchLabels',{})
        if selector.get('app') == old_app:
            selector['app'] = name
    annotations = {'traffic.mlops/source-id':source_id}
    return {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':name,'namespace':'traffic-mlops'},
            'spec':{'replicas':0,'selector':{'matchLabels':{'app':name}},
                    'strategy':{'type':'RollingUpdate','rollingUpdate':{'maxSurge':1,'maxUnavailable':0}} if role=='api' else {'type':'Recreate'},
                    'minReadySeconds':20 if role=='api' else 5,'progressDeadlineSeconds':1200,
                    'template':{'metadata':{'labels':labels,'annotations':annotations},'spec':pod}}}


def headless(name, app):
    return {'apiVersion':'v1','kind':'Service','metadata':{'name':name,'namespace':'traffic-mlops'},
            'spec':{'clusterIP':'None','publishNotReadyAddresses':False,'selector':{'app':app},
                    'ports':[{'name':'http','port':8000,'targetPort':8000}]}}


def preview():
    return {'apiVersion':'v1','kind':'Service','metadata':{'name':'api-preview','namespace':'traffic-mlops'},
            'spec':{'selector':{'app':'api'},'ports':[{'name':'http','port':8000,'targetPort':8000}]}}
