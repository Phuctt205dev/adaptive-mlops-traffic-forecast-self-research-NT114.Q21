"""Candidate helpers must work in the pinned legacy image before mutation."""
import builtins
from contextlib import nullcontext,redirect_stdout
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,Mock,patch

from scripts.cicd.capacity import staging_disk
from tests.integration.cluster_delivery import Suite,legacy_workload
from scripts.cicd.role_manifests import render,SLOTS

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
    def values(self,role):
        images={r:{'image':'ghcr.io/owner/backend@sha256:'+str(i)*64,'source_id':str(i)*64}
                for i,r in enumerate(('api','tree','neural','sync'),1)}
        current=render(role,SLOTS[role][0],images)
        old=legacy_workload(current,role,'legacy-'+role,'ghcr.io/owner/old@sha256:'+'9'*64)
        return current,old

    def test_legacy_cohort_does_not_inherit_native_cache_opt_in(self):
        for role in SLOTS:
            with self.subTest(role=role):
                current,old=self.values(role)
                pod=old['spec']['template']['spec']
                self.assertFalse(any(e['name']=='TRAFFIC_ARTIFACT_CACHE_BYTES'
                    for c in [*pod['containers'],*pod.get('initContainers',[])] for e in c.get('env',[])))
                if role=='api':
                    native=current['spec']['template']['spec']
                    self.assertEqual({e['name']:e.get('value') for e in native['initContainers'][-2]['env']}['TRAFFIC_CACHE_RESTORE_MODE'],'active')
                    self.assertIn('TRAFFIC_ARTIFACT_CACHE_BYTES',{e['name'] for e in native['containers'][0]['env']})
                    legacy_restore=next(c for c in pod['initContainers'] if c['name']=='restore-cache')
                    self.assertEqual({e['name']:e.get('value') for e in legacy_restore['env']}['TRAFFIC_CACHE_RESTORE_MODE'],'full')
                    self.assertEqual(pod['containers'][-1]['name'],'artifact-cache-sync')
                    self.assertNotIn('restartPolicy',pod['containers'][-1])

    def test_legacy_bootstrap_never_creates_metadata_for_old_uploader(self):
        from backend.app.services import restore_cache
        _,old=self.values('api');pod=old['spec']['template']['spec']
        init=next(c for c in pod['initContainers'] if c['name']=='restore-cache')
        env={e['name']:e['value'] for e in init['env'] if 'value' in e}
        env['TRAFFIC_API_RUNTIME_BUCKET']='synthetic'
        client=Mock();client.get_paginator.return_value.paginate.return_value=[{'Contents':[{'Key':'models/probe.joblib','Size':5}]}]
        client.get_object.return_value={'Body':io.BytesIO(b'model'),'ETag':'fixture-etag','Metadata':{}}
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,env,clear=True),\
             patch.object(restore_cache,'bootstrap_keys',return_value={'models/probe.joblib'}),\
             patch.dict(sys.modules,{'backend.app.services.region_cleanup_uploader':SimpleNamespace(get_upload_client=lambda:client)}),\
             patch('backend.app.services.artifact_cache.ArtifactCache',side_effect=AssertionError('Legacy must not create a native cache index')),\
             redirect_stdout(io.StringIO()):
            root=Path(directory);restore_cache.restore(root)
            self.assertEqual((root/'models/probe.joblib').read_bytes(),b'model')
            self.assertFalse((root/'models/.traffic-cache').exists())

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
