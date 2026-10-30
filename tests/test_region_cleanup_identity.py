"""Region deletion must recognize UUID-based tracking without crossing owners."""
import sys
from types import SimpleNamespace as Object
import unittest
from unittest.mock import Mock, patch
import uuid

from backend.app.core.errors import ApplicationError
from backend.app.services.region_cleanup_resources import CleanupResources


class RegionCleanupIdentityTests(unittest.TestCase):
    def setUp(self):
        self.rid = str(uuid.uuid4())
        self.other = str(uuid.uuid4())
        self.training = str(uuid.uuid4())
        self.resource = CleanupResources.__new__(CleanupResources)
        self.plan = {"region_id": self.rid, "region_name": "Test first time training"}
        self.run = Object(info=Object(run_id="a" * 32, artifact_uri="mlflow-artifacts:/6/run/artifacts"),
                          data=Object(tags={"traffic_region_id": self.rid, "traffic_training_run_id": self.training}))
        self.exp = Object(experiment_id="6", name="Traffic Forecast - " + self.rid,
                          artifact_location="mlflow-artifacts:/6", tags={"traffic_region_id": self.rid})

    def resolve(self, *, registered_owner=None, training_owner=None, additional_runs=()):
        client = Mock()
        client.search_experiments.return_value = [self.exp]
        page = type("RunPage", (list,), {"token": None})([self.run, *additional_runs])
        client.search_runs.return_value = page
        db = Mock()
        db.scalars.side_effect = [[Object(id=uuid.UUID(self.other), name="Another City")],
            [Object(mlflow_run_id=self.run.info.run_id, region_id=registered_owner)] if registered_owner else [],
            [Object(id=uuid.UUID(self.training), region_id=training_owner or self.rid)]]
        modules = {"mlflow": Object(), "mlflow.entities": Object(ViewType=Object(ALL="ALL")),
                   "mlflow.tracking": Object(MlflowClient=Mock(return_value=client))}
        with patch.dict(sys.modules, modules):
            result = self.resource._mlflow(db, self.plan)
        client.delete_experiment.assert_not_called()
        return result

    def test_uuid_experiment_with_new_tags_and_unregistered_model_is_owned(self):
        self.assertEqual(self.resolve(), [{"id": "6", "run_ids": ["a" * 32]}])

    def test_conflicting_old_and_new_region_tags_block_every_delete(self):
        self.run.data.tags["region_id"] = self.other
        with self.assertRaises(ApplicationError):
            self.resolve()

    def test_foreign_registered_model_or_training_identity_blocks_every_delete(self):
        with self.assertRaises(ApplicationError):
            self.resolve(registered_owner=self.other)
        with self.assertRaises(ApplicationError):
            self.resolve(training_owner=self.other)

    def test_conflicting_experiment_identity_is_not_overridden_by_its_name(self):
        self.exp.tags["traffic_region_id"] = self.other
        with self.assertRaises(ApplicationError):
            self.resolve()

    def test_unknown_owner_in_unrecognized_experiment_still_blocks(self):
        self.exp.name = "Renamed experiment"
        self.exp.tags = {}
        self.run.data.tags = {}
        with self.assertRaises(ApplicationError):
            self.resolve(registered_owner=self.rid, additional_runs=[
                Object(info=Object(run_id="b" * 32), data=Object(tags={}))])

    def test_unrelated_experiment_is_skipped(self):
        self.exp.name = "Unrelated experiment"
        self.exp.tags = {"traffic_region_id": self.other}
        self.run.data.tags = {"traffic_region_id": self.other}
        self.assertEqual(self.resolve(), [])
