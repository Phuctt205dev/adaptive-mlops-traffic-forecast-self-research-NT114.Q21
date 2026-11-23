"""Delete the stored files of the listed training runs, only while they are unreferenced.

Nothing is chosen automatically: every run is named in full (take them from the
read-only report, unreferenced_run_data.py). The default is a dry run that performs
every check and lists what would be removed. --execute deletes, one run at a time,
through the journaled cleanup (retryable, peers purge local copies, uploads are fenced).

Run inside an API pod, for example:
  kubectl exec -i <api-pod> -c api -- sh -c 'cd /app && PYTHONPATH=/app python -B - --run <uuid> [--execute]' < this-file
"""
import argparse
import sys
import uuid


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="append", required=True, help="full training run id (repeatable)")
    parser.add_argument("--execute", action="store_true", help="really delete; without it nothing changes")
    return parser.parse_args(argv)


def summarize(plan):
    mlflow = len(plan.get("mlflow_runs", []))
    return f"{len(plan['objects'])} storage locations, {mlflow} tracking run(s)"


def main(argv=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    from backend.app.core.errors import ApplicationError
    from backend.app.db.models import TrainingRun
    from backend.app.db.session import SessionLocal
    from backend.app.services import region_cleanup_resources
    from backend.app.services.run_data_cleanup import RunDataCleanupResources, delete_run_data, inventory

    # This process uses the deployed cleanup runtime: its DB barrier, peers and journal.
    region_cleanup_resources.INSTALLED = True
    failures = 0
    for raw in args.run:
        try:
            run_id = uuid.UUID(raw)
            if str(run_id) != raw.lower():
                raise ValueError("not canonical")
        except ValueError:
            print(f"{raw}: not a full training run id, skipped")
            failures += 1
            continue
        with SessionLocal() as db:
            run = db.get(TrainingRun, run_id)
            if run is None:
                print(f"{run_id}: training run not found, skipped")
                failures += 1
                continue
            try:
                if args.execute:
                    delete_run_data(db, run_id)
                    print(f"{run_id}: deleted (files removed, run record kept)")
                else:
                    plan = RunDataCleanupResources().preflight(db, inventory(db, run))
                    print(f"{run_id}: DRY RUN ok, would remove {summarize(plan)}")
            except ApplicationError as error:
                print(f"{run_id}: not changed - {error.code}: {error.message}")
                failures += 1
    print("finished" + (" with problems" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
