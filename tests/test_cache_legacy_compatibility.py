"""Candidate helpers must work in the pinned legacy image before mutation."""
import builtins
from contextlib import nullcontext
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch

from scripts.cicd.capacity import staging_disk
from tests.integration.cluster_delivery import Suite

ROOT=Path(__file__).resolve().parents[1]


class LegacyHelperTests(unittest.TestCase):
    def guard(self):
        original=builtins.__import__
        def legacy_import(name,*args,**kwargs):
            if name=='backend' or name.startswith('backend.'):
                raise ModuleNotFoundError('Candidate source is absent in the pinned legacy image')
            return original(name,*args,**kwargs)
        return legacy_import

    def helper(self):
        source=(ROOT/'backend/app/services/artifact_sync_control.py').read_text(encoding='utf-8')
        namespace={'__name__':'migration_sync_control'}
        exec(compile(source,'candidate-sync-control','exec'),namespace)
        return namespace

    def test_legacy_inventory_and_flush_need_no_candidate_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'models').mkdir();(root/'models/probe').write_bytes(b'synthetic')
            internal=root/'models/.traffic-cache';internal.mkdir();(internal/'index.json').write_text('{}')
            session=MagicMock();session.__enter__.return_value.scalars.return_value=[]
            class State:
                def __init__(self):self.acknowledged={}
            def upload(root,state):state.acknowledged['models/probe']=((1,2,3,4,5),0);return 1
            module=SimpleNamespace(database_barrier=lambda *_:nullcontext(),engine=object(),
                SessionLocal=lambda:session,pending_cleanup=lambda _:None,
                select=MagicMock(),AuditLog=MagicMock(),COMPLETED='completed',
                os=SimpleNamespace(environ={'TRAFFIC_API_RUNTIME_BUCKET':'synthetic'}),
                owns_key=lambda *_:False,file_fingerprint=lambda _:(1,2,3,4,5),
                UploadState=State,upload_cycle=upload)
            with patch.dict(os.environ,{},clear=True),patch('builtins.__import__',side_effect=self.guard()):
                helper=self.helper()
                self.assertEqual(helper['capabilities']()['schema'],'traffic-artifact-sync/v1')
                self.assertIsNone(helper['managed_cache'](root))
                self.assertEqual(set(helper['inventory'](root,module)),{'models/probe'})
                proof=helper['flush'](root,module=module)
            self.assertTrue(proof['complete']);self.assertEqual(proof['acknowledged_files'],1)

    def test_opted_in_runtime_does_not_silently_skip_missing_cache_module(self):
        with patch.dict(os.environ,{'TRAFFIC_ARTIFACT_CACHE_BYTES':'262144'},clear=True),\
             patch('builtins.__import__',side_effect=self.guard()):
            helper=self.helper()
            with self.assertRaises(ModuleNotFoundError):helper['managed_cache']('/not-accessed')


class FixtureStorageTests(unittest.TestCase):
    def test_disposable_pvc_replacement_is_bounded_and_capacity_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory);target=source/'deployment/rke2/roles';target.mkdir(parents=True)
            for role in ('api','tree','neural'):
                shutil.copyfile(ROOT/'deployment/rke2/roles'/(role+'.json'),target/(role+'.json'))
            shutil.copyfile(ROOT/'deployment/rke2/runtime-contract.json',target.parent/'runtime-contract.json')
            suite=object.__new__(Suite);suite.source=source;suite.profile()
            import json
            replacements=[]
            for role in ('api','tree','neural'):
                pod=json.loads((target/(role+'.json')).read_text())['spec']['template']['spec']
                replacements.extend(v for v in pod['volumes'] if v.get('emptyDir',{}).get('sizeLimit')=='16Mi')
                self.assertGreater(staging_disk(pod),0)
                self.assertFalse(any('persistentVolumeClaim' in v for v in pod['volumes']))
            self.assertTrue(replacements)


if __name__=='__main__':unittest.main()
