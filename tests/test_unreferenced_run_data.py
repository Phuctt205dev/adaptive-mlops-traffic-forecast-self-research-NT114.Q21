"""The report is read-only: the fake storage and tracking clients expose listing only."""
from types import SimpleNamespace as Obj
import unittest
import uuid

from sqlalchemy import delete, select, update

from backend.app.db.models import (
    AuditLog, DriftCheck, ModelVersion, Prediction, TrainingRun, TrainingRunStatus,
)
from backend.app.db.training_execution import TrainingExecution
from scripts.maintenance import unreferenced_run_data as report
from tests import test_region_cleanup as fixtures


class ListingOnlyS3:
    """Only get_paginator exists; any delete/put would raise AttributeError."""

    def __init__(self, objects):
        self.objects = {bucket: dict(items) for bucket, items in objects.items()}

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        outer = self

        class Paginator:
            def paginate(self, Bucket, Prefix="", Delimiter=None):
                keys = {key: size for key, size in outer.objects.get(Bucket, {}).items() if key.startswith(Prefix)}
                if Delimiter:
                    heads = {Prefix + key[len(Prefix):].split(Delimiter, 1)[0] + Delimiter
                             for key in keys if Delimiter in key[len(Prefix):]}
                    return [{"CommonPrefixes": [{"Prefix": head} for head in sorted(heads)]}]
                return [{"Contents": [{"Key": key, "Size": size} for key, size in sorted(keys.items())]}]

        return Paginator()


class ListingOnlyMlflow:
    def __init__(self, runs):
        self.runs = runs  # [(experiment, run_id, tags)]

    def search_experiments(self, view_type, max_results):
        return [Obj(experiment_id=e, lifecycle_stage="active") for e in sorted({r[0] for r in self.runs})]

    def search_runs(self, experiments, run_view_type, max_results):
        return [Obj(info=Obj(run_id=run_id, lifecycle_stage="active"), data=Obj(tags=tags))
                for experiment, run_id, tags in self.runs if experiment in experiments]


class UnreferencedRunDataTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    tearDown = fixtures.CleanupTests.tearDown
    make_region = fixtures.CleanupTests.make_region

    def extra_run(self, region, status=TrainingRunStatus.COMPLETED):
        run = TrainingRun(region_id=region.id, dataset_id=self.dataset.id, status=status,
                          split_policy="test", requested_by=self.user.id)
        self.db.add(run)
        self.db.commit()
        return run

    def build(self, objects=None, mlflow_runs=()):
        s3 = ListingOnlyS3({"runtime": objects or {}, "tracking": {}})
        for experiment, run_id, _ in mlflow_runs:
            s3.objects["tracking"][f"{experiment}/{run_id}/artifacts/model.pkl"] = 5 * 1024 * 1024
        return report.build_report(self.db, s3, ListingOnlyMlflow(list(mlflow_runs)),
                                   runtime_bucket="runtime", mlflow_bucket="tracking")

    def keys(self, region, run, size=1024 * 1024):
        locations = report.run_locations(region.id, run.id)
        return {locations["serving"] + "bundle/model.pkl": 10 * size, locations["exchange"] + "manifest.json": size}

    def test_run_without_a_model_is_listed_as_removable_and_the_model_run_is_kept(self):
        orphan = self.extra_run(self.region)
        objects = {**self.keys(self.region, self.run), **self.keys(self.region, orphan)}
        result = self.build(objects)
        by_id = {item["run_id"]: item for item in result["runs"]}
        self.assertFalse(by_id[str(self.run.id)]["removable"])
        self.assertIn("a registered model still uses this run", by_id[str(self.run.id)]["blockers"])
        self.assertTrue(by_id[str(orphan.id)]["removable"])
        self.assertEqual(by_id[str(orphan.id)]["bytes"], 11 * 1024 * 1024)
        self.assertEqual(result["removable_runs"], 1)
        self.assertEqual(result["removable_bytes"], 11 * 1024 * 1024)
        self.assertTrue(result["read_only"])

    def test_deleted_model_leaves_run_removable_only_after_the_model_is_gone(self):
        objects = self.keys(self.region, self.run)
        self.assertEqual(self.build(objects)["removable_runs"], 0)
        self.run.recommended_model_version_id = None
        self.region.active_model_version_id = None
        model = self.db.scalar(select(ModelVersion).where(ModelVersion.training_run_id == self.run.id))
        self.db.execute(delete(Prediction).where(Prediction.model_version_id == model.id))
        self.db.execute(update(DriftCheck).where(DriftCheck.active_model_version_id == model.id)
                        .values(active_model_version_id=None))
        self.db.delete(model)
        self.db.commit()
        self.assertEqual(self.build(objects)["removable_runs"], 1)

    def test_a_run_mentioned_by_a_live_model_is_never_a_candidate(self):
        orphan = self.extra_run(self.region)
        self.model.artifact_uri = report.run_locations(self.region.id, orphan.id)["serving"] + "bundle/model.pkl"
        self.db.commit()
        item = next(entry for entry in self.build(self.keys(self.region, orphan))["runs"]
                    if entry["run_id"] == str(orphan.id))
        self.assertFalse(item["removable"])
        self.assertIn("a live model's files or configuration point at this run", item["blockers"])

    def test_running_unfinished_or_pending_runs_are_never_candidates(self):
        running = self.extra_run(self.region, TrainingRunStatus.TRAINING)
        busy = self.extra_run(self.region)
        self.db.add(TrainingExecution(training_run_id=busy.id, backend="kaggle", state="running"))
        pending = self.extra_run(self.other)
        self.db.add(AuditLog(action="region_cleanup_pending", entity_type="region", entity_id=self.other.id, after_json={}))
        self.db.commit()
        objects = {}
        for item in (running, busy, pending):
            objects.update(self.keys(self.region if item is not pending else self.other, item))
        by_id = {item["run_id"]: item for item in self.build(objects)["runs"]}
        self.assertIn("the run is still training", by_id[str(running.id)]["blockers"])
        self.assertIn("the execution is not finished", by_id[str(busy.id)]["blockers"])
        self.assertIn("a deletion for this region or run is unfinished", by_id[str(pending.id)]["blockers"])
        self.assertFalse(any(item["removable"] for item in by_id.values()))

    def test_local_staging_and_tracking_runs_are_counted_and_untagged_ones_need_review(self):
        orphan = self.extra_run(self.region)
        tracked = uuid.uuid4().hex
        legacy = uuid.uuid4().hex
        objects = {report.run_locations(self.region.id, orphan.id)["local_staging"] + "input/data.csv": 3 * 1024 * 1024}
        result = self.build(objects, [("5", tracked, {"traffic_training_run_id": str(orphan.id)}),
                                      ("1", legacy, {})])
        item = next(entry for entry in result["runs"] if entry["run_id"] == str(orphan.id))
        self.assertEqual(item["areas"]["local_staging"]["bytes"], 3 * 1024 * 1024)
        self.assertEqual(item["areas"]["mlflow"][0]["bytes"], 5 * 1024 * 1024)
        self.assertEqual(item["bytes"], 8 * 1024 * 1024)
        self.assertEqual([entry["run"] for entry in result["needs_human_review"]], [legacy])
        self.assertNotIn(legacy, str(result["runs"]))

    def test_folders_for_unknown_runs_are_reported_but_not_attributed(self):
        stranger = uuid.uuid4()
        locations = report.run_locations(self.region.id, stranger)
        result = self.build({locations["serving"] + "bundle/model.pkl": 2 * 1024 * 1024})
        self.assertEqual([item["prefix"] for item in result["unknown_run_folders"]], [locations["serving"]])
        self.assertEqual(result["removable_runs"], 0)

    def test_text_summary_names_every_verdict(self):
        orphan = self.extra_run(self.region)
        text = report.describe(self.build({**self.keys(self.region, self.run), **self.keys(self.region, orphan)}))
        self.assertIn("READ-ONLY", text)
        self.assertIn("REMOVABLE", text)
        self.assertIn("KEEP (a registered model still uses this run", text)


if __name__ == "__main__":
    unittest.main()
