"""User-owned migration Job. Add the execution table in one DB transaction."""
import importlib.util
import json
from pathlib import Path
import sys

sys.path.insert(0, "/app")


def main():
    try:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import inspect, text
        from backend.app.db.session import engine
        path = Path("/opt/traffic-cloud/migration.py")
        spec = importlib.util.spec_from_file_location("cloud_execution_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(736280142901)"))
            heads = list(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
            if heads == [migration.revision]:
                required = {"training_run_id", "backend", "state", "cancel_requested", "dismissed", "error_code",
                            "error_message", "branches_json", "models_json", "events_json", "sequence", "created_at", "updated_at"}
                actual = {item["name"] for item in inspect(connection).get_columns("training_executions")}
                if actual != required:
                    raise RuntimeError("Existing migration schema differs.")
            elif heads == [migration.down_revision] and not inspect(connection).has_table("training_executions"):
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                connection.execute(text("UPDATE alembic_version SET version_num=:next WHERE version_num=:prior"),
                                   {"next": migration.revision, "prior": migration.down_revision})
            else:
                raise RuntimeError("Database migration head/schema differs from review.")
        print(json.dumps({"migration_ready": True, "revision": migration.revision}))
        return 0
    except Exception as error:
        print(json.dumps({"migration_ready": False, "error_type": type(error).__name__, "raw_details_omitted": True}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
