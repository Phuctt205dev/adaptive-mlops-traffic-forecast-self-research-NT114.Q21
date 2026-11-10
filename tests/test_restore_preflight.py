"""Real candidate inventory logic against synthetic listings, without S3 writes."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.app.services import restore_cache as cache
from scripts.cicd.roles_deploy import RolesDelivery


def client_for(*pages):
    client=Mock()
    client.get_paginator.return_value.paginate.return_value=iter(pages)
    return client


class RestoreInventoryTests(unittest.TestCase):
    def test_pages_prefix_filter_and_exact_limits_are_shared(self):
        client=client_for({'Contents':[{'Key':'models/a','Size':3},{'Key':'unrelated/b','Size':999}]},
                          {'Contents':[{'Key':'results/c','Size':2},{'Key':'models/','Size':0}]})
        entries,report=cache.inventory(client,'synthetic',max_bytes=5,max_files=2)
        self.assertEqual(entries,[('models/a',3),('results/c',2)])
        self.assertEqual((report['files'],report['bytes']),(2,5))
        self.assertTrue(report['within_budget']);self.assertTrue(report['complete'])
        client.get_object.assert_not_called()

    def test_file_and_byte_overflow_report_lower_bound_without_object_names(self):
        for budgets in ({'max_files':1,'max_bytes':100},{'max_files':100,'max_bytes':2}):
            client=client_for({'Contents':[{'Key':'models/private-model','Size':2},{'Key':'models/another-private','Size':1}]})
            report=cache.inventory_report(client,'synthetic',**budgets)
            self.assertFalse(report['within_budget']);self.assertFalse(report['complete'])
            self.assertEqual((report['files'],report['bytes']),(2,3))
            self.assertNotIn('private',json.dumps(report));client.get_object.assert_not_called()

    def test_overflow_stops_listing_instead_of_collecting_unbounded_history(self):
        def pages():
            yield {'Contents':[{'Key':'models/a','Size':3}]}
            raise AssertionError('Listing continued after exceeding reviewed budget')
        client=Mock();client.get_paginator.return_value.paginate.return_value=pages()
        report=cache.inventory_report(client,'synthetic',max_bytes=2)
        self.assertEqual(report['bytes'],3)

    def test_invalid_keys_sizes_and_incomplete_listing_fail_closed(self):
        for entry in ({'Key':'models/../escape','Size':1},{'Key':'models/a','Size':-1},
                      {'Key':'models/a','Size':True},{'Key':'models/a','Size':'1'}):
            with self.subTest(entry=entry),self.assertRaises((ValueError,RuntimeError)):
                cache.inventory_report(client_for({'Contents':[entry]}),'synthetic')
        client=Mock();client.get_paginator.return_value.paginate.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):cache.inventory_report(client,'synthetic')

    def test_init_also_refuses_overflow_before_any_download(self):
        client=client_for({'Contents':[{'Key':'models/a','Size':3}]})
        module=SimpleNamespace(get_upload_client=lambda:client)
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'TRAFFIC_API_RUNTIME_BUCKET':'synthetic'}), \
             patch.dict(sys.modules,{'backend.app.services.region_cleanup_uploader':module}):
            with self.assertRaises(cache.RestoreBudgetExceeded):cache.restore(Path(directory),max_bytes=2)
        client.get_object.assert_not_called()


class CandidatePreflightTests(unittest.TestCase):
    def operator(self,root):
        op=object.__new__(RolesDelivery)
        op.directory=root;op.path=root/'receipt.json'
        op.state={'patched':[],'started':False,'guard_held':False}
        op.cluster=Mock();op.cluster.pod.return_value='existing-api'
        candidate=root/'roles/sync/backend/app/services/restore_cache.py'
        candidate.parent.mkdir(parents=True)
        candidate.write_text(Path(cache.__file__).read_text(encoding='utf-8'),encoding='utf-8')
        return op

    def test_preflight_executes_checked_candidate_on_existing_sidecar_and_preserves_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);op=self.operator(root)
            client=client_for({'Contents':[{'Key':'models/synthetic','Size':1}]})
            def execute(pod,container,source):
                self.assertEqual((pod,container),('existing-api','artifact-sync'))
                with patch.dict(os.environ,{'TRAFFIC_API_RUNTIME_BUCKET':'synthetic'}), \
                     patch.dict(sys.modules,{'backend.app.services.region_cleanup_uploader':SimpleNamespace(get_upload_client=lambda:client)}), \
                     redirect_stdout(io.StringIO()) as output:
                    exec(source,{})
                return json.loads(output.getvalue())
            op.execute=Mock(side_effect=execute)
            op.restore_preflight({'metadata':{'name':'api'}})
            self.assertEqual(op.state['restore_inventory']['files'],1)
            self.assertEqual(op.state['patched'],[])
            op.cluster.call.assert_not_called();client.get_object.assert_not_called()

    def test_budget_failure_in_plan_never_pauses_or_mutates(self):
        with tempfile.TemporaryDirectory() as directory:
            op=self.operator(Path(directory))
            op.execute=Mock(return_value={'complete':False,'within_budget':False,'files':20001})
            op.plan=Mock(side_effect=lambda:op.restore_preflight({}))
            op.pause=Mock();op.change=Mock();op.start_guard=Mock()
            with self.assertRaisesRegex(RuntimeError,'preflight'):op.apply()
            op.pause.assert_not_called();op.change.assert_not_called();op.start_guard.assert_not_called()
            self.assertFalse(op.state['started'])

    def test_legacy_migration_uses_reviewed_loader_and_legacy_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            op=self.operator(Path(directory))
            op.migration=Mock();op.migration.legacy.return_value=True
            op.migration.plan={'api_sidecar':'artifact-cache-sync'}
            op.migration.source.return_value='checked legacy loader and inventory'
            op.execute=Mock(return_value={'complete':True,'within_budget':True})
            op.restore_preflight({'metadata':{'name':'traffic-api'}})
            op.migration.source.assert_called_once_with('inventory')
            op.execute.assert_called_once_with('existing-api','artifact-cache-sync','checked legacy loader and inventory')


if __name__=='__main__':unittest.main()
