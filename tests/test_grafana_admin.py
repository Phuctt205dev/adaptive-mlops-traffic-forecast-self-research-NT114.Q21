"""Dashboard updates keep backups and reject source/server drift before writes."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.observability import grafana_admin as admin


class FakeGrafana:
    def __init__(self):
        self.documents = {uid: {"meta": {"provisioned": False, "folderUid": "existing-folder"},
            "dashboard": {"uid": uid, "title": filename, "version": 7, "id": index,
                "panels": [{"id": 1, "title": "Before", "targets": [{"refId": "A", "expr": "vector(1)"}]}]}}
            for index, (uid, filename) in enumerate(admin.UID_FILES.items(), 1)}
        self.writes = []

    def get(self, path):
        return copy.deepcopy(self.documents[path.rsplit("/", 1)[1]])

    def requests(self, requests):
        self.writes.extend(copy.deepcopy(requests))
        for request in requests:
            dashboard = copy.deepcopy(request["body"]["dashboard"])
            dashboard["version"] += 1
            self.documents[dashboard["uid"]]["dashboard"] = dashboard
        return [{"status": 200, "body": {"status": "success"}} for _ in requests]


class GrafanaAdminTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "review"
        path = self.root / "deployment/observability/dashboards"
        path.mkdir(parents=True)
        self.client = FakeGrafana()
        self.before = copy.deepcopy(self.client.documents)
        for uid, filename in admin.UID_FILES.items():
            desired = copy.deepcopy(self.client.documents[uid]["dashboard"])
            desired["panels"][0]["title"] = "After"
            desired["version"] = 0
            desired["id"] = None
            (path / filename).write_text(json.dumps(desired), encoding="utf-8")
        self.addCleanup(patch.stopall)
        patch.object(admin, "ROOT", self.root).start()
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        admin.plan(self.client, self.directory)

    def test_plan_backs_up_both_dashboards_without_writes(self):
        for uid in admin.UID_FILES:
            self.assertEqual(json.loads((self.directory / ("before-" + uid + ".json")).read_text()), self.before[uid])
        self.assertEqual(self.client.writes, [])

    def test_changed_remote_dashboard_blocks_the_entire_update(self):
        self.client.documents["traffic-rke2-nodes"]["dashboard"]["version"] += 1
        with self.assertRaisesRegex(RuntimeError, "changed"):
            admin.apply(self.client, self.directory)
        self.assertEqual(self.client.writes, [])

    def test_changed_local_source_blocks_the_entire_update(self):
        filename = self.root / "deployment/observability/dashboards/nodes.json"
        document = json.loads(filename.read_text())
        document["panels"][0]["title"] = "Unreviewed"
        filename.write_text(json.dumps(document))
        with self.assertRaisesRegex(RuntimeError, "changed"):
            admin.apply(self.client, self.directory)
        self.assertEqual(self.client.writes, [])

    def test_update_uses_optimistic_version_and_keeps_folder_then_verifies(self):
        admin.apply(self.client, self.directory)
        self.assertEqual(len(self.client.writes), 2)
        for request in self.client.writes:
            self.assertFalse(request["body"]["overwrite"])
            self.assertEqual(request["body"]["folderUid"], "existing-folder")
            self.assertEqual(request["body"]["dashboard"]["version"], 7)
        admin.verify(self.client, self.directory)
        proof = json.loads((self.directory / "dashboard-verify.json").read_text())
        self.assertTrue(all(item["panels_and_queries_match"] for item in proof))

    def test_unknown_dashboard_in_plan_is_rejected(self):
        path = self.directory / "dashboard-plan.json"
        review = json.loads(path.read_text())
        review["entries"][0]["uid"] = "unrelated-dashboard"
        path.write_text(json.dumps(review))
        with self.assertRaisesRegex(RuntimeError, "Only the two"):
            admin.apply(self.client, self.directory)
        self.assertEqual(self.client.writes, [])

    def test_rollback_refuses_new_visual_edits_even_when_queries_match(self):
        admin.apply(self.client, self.directory)
        self.client.documents["traffic-system-overview"]["dashboard"]["panels"][0]["title"] = "New admin edit"
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "drifted"):
            admin.rollback(self.client, self.directory)
        self.assertEqual(self.client.writes, [])

    def test_rollback_checks_both_dashboards_before_writing_either(self):
        admin.apply(self.client, self.directory)
        self.client.documents["traffic-rke2-nodes"]["dashboard"]["panels"][0]["title"] = "New admin edit"
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "drifted"):
            admin.rollback(self.client, self.directory)
        self.assertEqual(self.client.writes, [])

    def test_rollback_restores_reviewed_content_without_forcing_version(self):
        admin.apply(self.client, self.directory)
        admin.rollback(self.client, self.directory)
        for uid, document in self.before.items():
            self.assertEqual(self.client.documents[uid]["dashboard"]["panels"], document["dashboard"]["panels"])
            self.assertEqual(self.client.documents[uid]["dashboard"]["version"], 9)


if __name__ == "__main__":
    unittest.main()
