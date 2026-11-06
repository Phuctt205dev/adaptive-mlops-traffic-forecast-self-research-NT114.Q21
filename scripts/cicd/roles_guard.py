"""Short-lived deployment/peer leases; never acquire the serving barrier."""
import json
from pathlib import Path
import time

STATUS = Path('/tmp/role-guard-status.json')
RELEASE = Path('/tmp/role-guard-release')
PEER = Path('/tmp/role-guard-peer')
DEPLOY_KEY = 73918046225
PEER_KEY = 73918046226


def write(**values):
    temporary = STATUS.with_suffix('.tmp')
    temporary.write_text(json.dumps(values))
    temporary.replace(STATUS)


def main():
    from sqlalchemy import create_engine, text
    from backend.app.core.config import get_settings
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    peer = False
    with engine.connect() as connection:
        acquired = connection.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': DEPLOY_KEY}).scalar_one()
        connection.commit()
        if not acquired:
            raise RuntimeError('Another worker admission transaction is active')
        try:
            heads = list(connection.execute(text('SELECT version_num FROM alembic_version')).scalars())
            active = connection.execute(text("SELECT count(*) FROM training_executions WHERE state IN ('running','finalizing','cancelling')")).scalar_one()
            connection.commit()
            if heads != ['20261028_01'] or active:
                raise RuntimeError('Active training or schema prevents worker replacement')
            write(state='held', peer_held=False, serving_blocked=False)
            deadline = time.monotonic() + 2400
            while not RELEASE.exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError('Deployment lease exceeded its reviewed budget')
                if PEER.exists() and not peer:
                    peer = connection.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': PEER_KEY}).scalar_one()
                    connection.commit()
                    if peer:
                        write(state='held', peer_held=True, serving_blocked=False)
                elif not PEER.exists() and peer:
                    connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': PEER_KEY})
                    connection.commit()
                    peer = False
                    write(state='held', peer_held=False, serving_blocked=False)
                connection.execute(text('SELECT 1'))
                connection.commit()
                time.sleep(.25)
        finally:
            if peer:
                connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': PEER_KEY})
            connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': DEPLOY_KEY})
            connection.commit()
            write(state='released', peer_held=False, serving_blocked=False)
    engine.dispose()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'guard_failed': True, 'error_type': type(error).__name__}), flush=True)
        raise SystemExit(1)
