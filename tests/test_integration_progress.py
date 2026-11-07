"""An interrupted hosted job retains evidence without claiming acceptance."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.integration import cluster_delivery as integration


class InterruptedAcceptance(unittest.TestCase):
    def test_case_start_is_durable_even_if_operator_construction_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            suite=object.__new__(integration.Suite)
            suite.cases=[{'case':'preview_smoke_rollback','rolled_back':True}]
            suite.cluster=None;suite.temp=Path(directory);suite.kubeconfig=Path(directory)/'isolated-config'
            with patch.object(integration,'ROOT',Path(directory)),patch.object(integration,'RolesDelivery',side_effect=RuntimeError('interrupted')):
                with self.assertRaisesRegex(RuntimeError,'interrupted'):
                    suite.operator((Path(directory),Path(directory)/'binding'),'manifest-change')
            proof=json.loads((Path(directory)/'results/cicd/kubernetes-delivery-progress.json').read_text())
            self.assertEqual(proof['current_case'],'manifest-change')
            self.assertEqual(proof['completed_cases'],suite.cases)
            self.assertEqual(proof['schema'],'traffic-isolated-delivery-progress/v1')
            self.assertFalse(proof['complete']);self.assertFalse(proof['production_accessed'])
            self.assertFalse(list((Path(directory)/'results/cicd').glob('*.tmp')))


if __name__=='__main__':unittest.main()
