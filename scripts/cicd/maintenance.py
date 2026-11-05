"""Run only inside the short-lived deployment guard pod.

Reuse the application's existing shared/exclusive PostgreSQL barrier so a
training request cannot enter between an idle check and pod replacement.
This program performs no application data writes or training.
"""
import hashlib
import json
from pathlib import Path
import sys
import time

LOCK_KEY = 73918046215
APP_ROOT = Path("/app")
STATUS = Path("/tmp/delivery-status.json")
RELEASE = Path("/tmp/delivery-release")


def load_runtime(app_root=None):
    """A file executed from /opt must explicitly find the pinned /app code."""
    root = Path(app_root) if app_root is not None else APP_ROOT
    if not (root / "backend/app/core/config.py").is_file():
        raise RuntimeError("The pinned application configuration is missing.")
    source = str(root.resolve())
    if source not in sys.path:
        sys.path.insert(0, source)
    from sqlalchemy import create_engine, text
    from backend.app.core.config import get_settings
    return create_engine, text, get_settings


def check_imports():
    """Image build smoke check only: no settings evaluation or DB connection."""
    try:
        load_runtime()
        print(json.dumps({"maintenance_imports_ready": True}), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({"maintenance_imports_ready": False, "phase": "imports", "error_type": type(error).__name__}), flush=True)
        return 1


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
    engine = None
    phase = "imports"
    try:
        create_engine, text, get_settings = load_runtime()
        phase = "configuration"
        engine = create_engine(get_settings().database_url, pool_pre_ping=True)
        phase = "connection"
        with engine.connect() as connection:
            phase = "lock"
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
            phase = "activity"
            heads = list(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
            pending = connection.execute(text("SELECT count(*) FROM training_runs WHERE status IN ('queued','training')")).scalar_one()
            pending += connection.execute(text("SELECT count(*) FROM training_executions WHERE state IN ('queued','running','finalizing','cancelling')")).scalar_one()
            connection.commit()
            if pending or heads != ["20261028_01"]:
                raise RuntimeError("Training or schema prevents maintenance.")
            phase = "identity"
            original = fingerprint(connection)
            write_status(state="held", fingerprint=original)
            print(json.dumps({"maintenance": "held"}), flush=True)
            phase = "held"
            # The Job deadline bounds recovery if the runner disappears. Never
            # resume Airflow here; only the delivery transaction knows its state.
            while not RELEASE.exists():
                connection.execute(text("SELECT 1"))
                connection.commit()
                time.sleep(2)
            phase = "release"
            unchanged = fingerprint(connection) == original
            write_status(state="released", data_unchanged=unchanged)
            for key in (LOCK_KEY, LOCK_KEY + 2):
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()
            if not unchanged:
                raise RuntimeError("Application identity changed during deployment.")
            print(json.dumps({"maintenance": "released", "data_unchanged": True}), flush=True)
    except Exception as error:
        write_status(state="failed", phase=phase, error_type=type(error).__name__)
        print(json.dumps({"maintenance": "failed", "phase": phase, "error_type": type(error).__name__}), flush=True)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-imports", action="store_true")
    args = parser.parse_args()
    raise SystemExit(check_imports() if args.check_imports else main())
