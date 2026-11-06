"""Anonymous ingress checks. No tokens, sessions, DB or application imports."""
import json
import time


def check(base):
    import requests
    durations=[]
    with requests.Session() as client:
        client.trust_env=False
        client.headers['ngrok-skip-browser-warning']='true'
        for path,expected in [('/health/live','ok'),('/health/ready','ready')]:
            start=time.monotonic()
            response=client.get(base.rstrip('/')+path,timeout=(3,10))
            if response.status_code!=200 or response.json().get('status')!=expected:
                raise RuntimeError('Anonymous public health verification failed')
            durations.append(round(time.monotonic()-start,3))
    return {'read_only':True,'health_only':True,'credential_sent':False,'requests':2,
            'errors':0,'max_seconds':max(durations)}
