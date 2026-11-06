"""Read-only HTTP checks, executed inside a serving pod. Never print credentials."""
import json
import time


def check(base, *, light=False):
    import requests
    from sqlalchemy import select
    from backend.app.core.security import create_access_token
    from backend.app.db.models import User, UserRole, Region
    from backend.app.db.session import SessionLocal
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.is_active.is_(True), User.role == UserRole.ADMIN))
        if user is None:
            raise RuntimeError('A read-only administrator probe identity is unavailable')
        token = create_access_token(user.id, user.role.value)
        regions = list(db.scalars(select(Region).where(Region.is_active.is_(True), Region.active_model_version_id.is_not(None))))
        paths = ['/admin/regions', '/admin/training-monitor/capabilities']
        for region in regions:
            key = str(region.id)
            paths.append('/regions/'+key+'/prediction-window')
            if not light:
                paths.extend(['/regions/'+key+'/forecast-dashboard', '/admin/regions/'+key+'/model-quality?hours=24',
                              '/admin/regions/'+key+'/feature-importance'])
    durations = []
    with requests.Session() as client:
        client.trust_env = False
        client.headers['Authorization'] = 'Bearer ' + token
        client.headers['ngrok-skip-browser-warning'] = 'true'
        for path in paths:
            started = time.monotonic()
            response = client.get(base.rstrip('/') + path, timeout=(3, 10 if light else 90))
            if response.status_code != 200:
                raise RuntimeError('Read-only business verification failed: '+path.rsplit('/',1)[-1]+' (HTTP '+str(response.status_code)+')')
            response.json()
            durations.append(round(time.monotonic()-started, 3))
    return {'read_only': True, 'requests':len(paths), 'active_regions':len(regions),
            'max_seconds':max(durations, default=0), 'errors':0}


if __name__ == '__main__':
    import sys
    print(json.dumps(check(sys.argv[1])))
