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
    # Summing sequential init containers too is intentionally conservative.
    result={'cpu':Decimal(0),'memory':Decimal(0)}
    for container in [*pod.get('containers',[]),*pod.get('initContainers',[])]:
        for resource in result:
            result[resource]+=quantity(container.get('resources',{}).get('requests',{}).get(resource,0))
    return result


def verify(pods,assignments,hosts,*,minimum_disk_bytes=12*1024**3):
    current={n:{'cpu':Decimal(0),'memory':Decimal(0)} for n in hosts}
    for pod in pods:
        node=pod['spec'].get('nodeName')
        if node not in current or pod.get('status',{}).get('phase') in {'Succeeded','Failed'}:continue
        resources=requests(pod['spec'])
        for key in resources:current[node][key]+=resources[key]
    extra={n:{'cpu':Decimal(0),'memory':Decimal(0)} for n in hosts}
    for node,spec in assignments:
        if node not in hosts:raise RuntimeError('No reviewed serving-node capacity probe for requested placement')
        for key,value in requests(spec).items():extra[node][key]+=value
    proof={}
    for node,host in hosts.items():
        if not extra[node]['memory'] and not extra[node]['cpu']:continue
        if current[node]['cpu']+extra[node]['cpu']+Decimal('.25')>host['cpus']:
            raise RuntimeError('Staging CPU requests exceed the serving-node budget')
        if extra[node]['memory']+512*1024**2>host['memory_available']:
            raise RuntimeError('Insufficient free memory for staged workloads')
        if host['disk_available']<minimum_disk_bytes:
            raise RuntimeError('A serving node lacks staging disk headroom')
        proof[node]={'additional_cpu_request':str(extra[node]['cpu']),
                     'additional_memory_request':int(extra[node]['memory']),
                     'memory_available':host['memory_available'],'disk_available':host['disk_available']}
    return {'basis':'namespace_requests_and_live_host_headroom_with_reserve','nodes':proof,
            'scheduler_admission_still_required':True}
