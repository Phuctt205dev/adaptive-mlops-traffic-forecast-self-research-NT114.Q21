"""Dashboard catalog changes stay limited to reviewed ConfigMap fields."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.observability import catalog


class CatalogClient:
    def __init__(self):
        self.maps = {}
        for name, title in {**catalog.REMOVE, "traffic-monitoring-prometheus": "Prometheus / Overview"}.items():
            self.maps[name] = {"metadata": {"name": name, "uid": "id-" + name, "resourceVersion": "1",
                "labels": {"grafana_dashboard": "1"}},
                "data": {"dashboard.json": json.dumps({"title": title, "uid": name, "panels": []})}}
        self.maps[catalog.PROVIDER] = {"metadata": {"name": catalog.PROVIDER, "uid": "provider", "resourceVersion": "1"},
            "data": {"provider.yaml": "folder: ''\ndisableDeletion: false\npath: /tmp/dashboards\nfoldersFromFilesStructure: false"}}

    def read(self, *args):
        return copy.deepcopy({"items": list(self.maps.values())} if args[1] == "configmaps" else self.maps[args[2]])


class GrafanaCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.client = CatalogClient()
        self.before = copy.deepcopy(self.client.maps)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        catalog.plan(self.client, self.directory)

    def test_plan_retains_sources_and_disables_only_three_irrelevant_dashboards(self):
        review = catalog.reviewed(self.directory)
        removed = {e["name"] for e in review["entries"] if e["operation"] == "disable-dashboard"}
        self.assertEqual(removed, set(catalog.REMOVE))
        self.assertEqual(self.client.maps, self.before)
        for entry in review["entries"]:
            self.assertEqual(json.loads((self.directory / entry["backup"]).read_text()), self.before[entry["name"]])

    def test_drift_is_detected_before_any_configmap_mutation(self):
        self.client.maps["traffic-monitoring-prometheus"]["metadata"]["resourceVersion"] = "2"
        with patch.object(catalog, "mutate") as mutate, self.assertRaisesRegex(RuntimeError, "changed"):
            catalog.apply(self.client, self.directory)
        mutate.assert_not_called()

    def test_dry_run_uses_server_validation_without_persisting(self):
        with patch.object(catalog, "mutate") as mutate:
            catalog.dry_run(self.client, self.directory)
        self.assertEqual(mutate.call_count, 5)
        self.assertTrue(all(call.kwargs == {"dry_run": True} for call in mutate.call_args_list))
        self.assertEqual(self.client.maps, self.before)

    def test_plan_cannot_patch_workload_or_general_configuration_fields(self):
        path = self.directory / "catalog-plan.json"
        review = json.loads(path.read_text())
        review["entries"][0]["patch"].append({"op": "add", "path": "/data/prometheus.yml", "value": "unreviewed"})
        path.write_text(json.dumps(review))
        with self.assertRaisesRegex(RuntimeError, "Unexpected field"):
            catalog.reviewed(self.directory)


if __name__ == "__main__":
    unittest.main()
