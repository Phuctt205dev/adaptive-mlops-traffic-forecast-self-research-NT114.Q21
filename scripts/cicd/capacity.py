"""Conservative staging requests checked against serving-node host headroom.

The runner needs no cluster-wide pod or Secret permission. Namespace requests
plus a reserve are checked against host CPU; MemAvailable/disk checks are live
headroom checks. Scheduler admission remains authoritative for other namespaces.
"""
from decimal import Decimal
import re


def quantity(value):
    match=re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([KMGTPE]i?|m)?',str(value))
    if not match:raise ValueError('Unsupported staging resource quantity')
    number=Decimal(match[1]);unit=match[2]
    if unit=='m':return number/1000
    if unit:
        number*=Decimal(1024 if unit.endswith('i') else 1000)**('KMGTPE'.index(unit[0])+1)
    return number


def requests(pod):
    # CPU/RAM sum sequential init containers conservatively. Storage requests
    # include the shared cache, so use the effective restartable-init request.
    result={'cpu':Decimal(0),'memory':Decimal(0),'ephemeral-storage':Decimal(0)}
    for container in [*pod.get('containers',[]),*pod.get('initContainers',[])]:
        for resource in result:
            result[resource]+=quantity(container.get('resources',{}).get('requests',{}).get(resource,0))
    sidecars=peak=Decimal(0)
    for container in pod.get('initContainers',[]):
        value=quantity(container.get('resources',{}).get('requests',{}).get('ephemeral-storage',0))
        if container.get('restartPolicy')=='Always':sidecars+=value
        peak=max(peak,sidecars+(0 if container.get('restartPolicy')=='Always' else value))
    app=sum((quantity(c.get('resources',{}).get('requests',{}).get('ephemeral-storage',0))
             for c in pod.get('containers',[])),Decimal(0))
    result['ephemeral-storage']=max(app+sidecars,peak)
    return result


def staging_disk(pod):
    """Reserve full disk emptyDir capacity plus writable layers/log requests.

    Existing usage is already reflected in live free disk. This is additional
    staging demand, not a second subtraction of all serving cache usage.
    """
    volumes=Decimal(0)
    for volume in pod.get('volumes',[]):
        empty=volume.get('emptyDir')
        if empty is not None and empty.get('medium')!='Memory':
            if not empty.get('sizeLimit'):
                raise RuntimeError('Staged disk cache needs a bounded sizeLimit')
            size=quantity(empty['sizeLimit'])
            if size<=0:raise ValueError('Invalid staging disk capacity')
            volumes+=size
    # Storage requests already cover emptyDir usage; don't count cache twice.
    # Leave another 512Mi per staged pod for writes/logs, in addition to the
    # node-level reserve (also covers shared image pulls and other workloads).
    return max(volumes,requests(pod)['ephemeral-storage'])+512*1024**2


def verify(pods,assignments,hosts,*,minimum_disk_bytes=12*1024**3):
    current={n:{'cpu':Decimal(0),'memory':Decimal(0),'ephemeral-storage':Decimal(0)} for n in hosts}
    for pod in pods:
        node=pod['spec'].get('nodeName')
        if node not in current or pod.get('status',{}).get('phase') in {'Succeeded','Failed'}:continue
        resources=requests(pod['spec'])
        for key in resources:current[node][key]+=resources[key]
    extra={n:{'cpu':Decimal(0),'memory':Decimal(0),'ephemeral-storage':Decimal(0)} for n in hosts}
    disk={n:Decimal(0) for n in hosts}
    for node,spec in assignments:
        if node not in hosts:raise RuntimeError('No reviewed serving-node capacity probe for requested placement')
        for key,value in requests(spec).items():extra[node][key]+=value
        disk[node]+=staging_disk(spec)
    proof={}
    for node,host in hosts.items():
        if not disk[node] and not any(extra[node].values()):continue
        if current[node]['cpu']+extra[node]['cpu']+Decimal('.25')>host['cpus']:
            raise RuntimeError('Staging CPU requests exceed the serving-node budget')
        if extra[node]['memory']+512*1024**2>host['memory_available']:
            raise RuntimeError('Insufficient free memory for staged workloads')
        if host['disk_available']<disk[node]+minimum_disk_bytes:
            raise RuntimeError('A serving node lacks staging disk headroom')
        proof[node]={'additional_cpu_request':str(extra[node]['cpu']),
                     'additional_memory_request':int(extra[node]['memory']),
                     'memory_available':host['memory_available'],'disk_available':host['disk_available'],
                     'additional_ephemeral_request':int(extra[node]['ephemeral-storage']),
                     'additional_disk_budget':int(disk[node]),'remaining_disk_reserve':minimum_disk_bytes}
    return {'basis':'namespace_requests_and_live_host_headroom_with_reserve','nodes':proof,
            'scheduler_admission_still_required':True}
