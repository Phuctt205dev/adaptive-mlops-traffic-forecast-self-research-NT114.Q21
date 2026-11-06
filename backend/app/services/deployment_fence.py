"""Admission fence for deployment vs new worker attempts; serving stays open."""
from contextlib import contextmanager
import time

from backend.app.core.errors import ApplicationError

LOCK_KEY = 73918046225
PEER_KEY = 73918046226


@contextmanager
def peer_membership(engine, *, exclusive=False, timeout=120):
    """Wait for cohort changes without holding the shared business barrier."""
    from sqlalchemy import text
    suffix = '' if exclusive else '_shared'
    with engine.connect() as connection:
        deadline = time.monotonic() + timeout
        while True:
            acquired = connection.execute(text('SELECT pg_try_advisory_lock' + suffix + '(:key)'), {'key': PEER_KEY}).scalar_one()
            connection.commit()
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise ApplicationError('peer_transition_busy', 'The service transition could not finish within its budget.', 503)
            time.sleep(.1)
        try:
            yield
        finally:
            connection.execute(text('SELECT pg_advisory_unlock' + suffix + '(:key)'), {'key': PEER_KEY})
            connection.commit()


@contextmanager
def execution_admission(engine):
    from sqlalchemy import text
    if engine.dialect.name != 'postgresql':
        raise ApplicationError('deployment_fence_unavailable', 'Worker admission requires PostgreSQL.', 503)
    with engine.connect() as connection:
        acquired = connection.execute(text('SELECT pg_try_advisory_lock_shared(:key)'), {'key': LOCK_KEY}).scalar_one()
        connection.commit()
        if not acquired:
            raise ApplicationError('training_worker_busy', 'The worker is preparing an update. The queued request will retry.', 503)
        try:
            yield
        finally:
            connection.execute(text('SELECT pg_advisory_unlock_shared(:key)'), {'key': LOCK_KEY})
            connection.commit()
