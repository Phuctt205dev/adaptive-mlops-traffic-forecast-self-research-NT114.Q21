"""Run only inside the short-lived deployment guard pod.

Reuse the application's existing shared/exclusive PostgreSQL barrier so a
training request cannot enter between an idle check and pod replacement.
This program performs no application data writes or training.
"""
import hashlib
import json
from pathlib import Path
import time

LOCK_KEY = 73918046215
STATUS = Path("/tmp/delivery-status.json")
RELEASE = Path("/tmp/delivery-release")


def write_status(**values):
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(values), encoding="utf-8")
    temporary.replace(STATUS)


def fingerprint(connection):
    from sqlalchemy import text
    result = {}
    for name, statement in {
        "active_models": "SELECT id, active_model_version_id FROM regions ORDER BY id",
        "datasets": "SELECT id, sha256 FROM datasets ORDER BY id",
    }.items():
        rows = [[str(v) if v is not None else None for v in row] for row in connection.execute(text(statement))]
        result[name] = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    connection.commit()
    return result


def main():
    from sqlalchemy import create_engine, text
    from backend.app.core.config import get_settings
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            for key in (LOCK_KEY + 2, LOCK_KEY):
                acquired = False
                for _ in range(20):
                    acquired = connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar_one()
                    connection.commit()
                    if acquired:
                        break
                    time.sleep(1)
                if not acquired:
                    raise RuntimeError("Application activity prevents maintenance.")
            heads = list(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
            pending = connection.execute(text("SELECT count(*) FROM training_runs WHERE status IN ('queued','training')")).scalar_one()
            pending += connection.execute(text("SELECT count(*) FROM training_executions WHERE state IN ('queued','running','finalizing','cancelling')")).scalar_one()
            connection.commit()
            if pending or heads != ["20261028_01"]:
                raise RuntimeError("Training or schema prevents maintenance.")
            original = fingerprint(connection)
            write_status(state="held", fingerprint=original)
            print(json.dumps({"maintenance": "held"}), flush=True)
            # The Job deadline bounds recovery if the runner disappears. Never
            # resume Airflow here; only the delivery transaction knows its state.
            while not RELEASE.exists():
                connection.execute(text("SELECT 1"))
                connection.commit()
                time.sleep(2)
            unchanged = fingerprint(connection) == original
            write_status(state="released", data_unchanged=unchanged)
            for key in (LOCK_KEY, LOCK_KEY + 2):
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()
            if not unchanged:
                raise RuntimeError("Application identity changed during deployment.")
            print(json.dumps({"maintenance": "released", "data_unchanged": True}), flush=True)
    except Exception as error:
        write_status(state="failed", error_type=type(error).__name__)
        print(json.dumps({"maintenance": "failed", "error_type": type(error).__name__}), flush=True)
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
