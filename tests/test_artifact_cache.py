"""Cold startup and disk eviction with synthetic S3; never contacts a provider."""
from contextlib import redirect_stdout,nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from backend.app.services import restore_cache as restore
from backend.app.services.artifact_cache import ArtifactCache
from backend.app.services.serving_runtime import PredictionRuntime,install_neural_restore
from backend.app.core.errors import ApplicationError
from scripts.cicd.capacity import requests,staging_disk,verify


class Objects:
    def __init__(self,files):
        self.files=files;self.downloaded=[];self.headed=[]
    def head_object(self,*,Bucket,Key):
        self.headed.append(Key);body=self.files[Key]
        return {'ContentLength':len(body),'ETag':hashlib.md5(body).hexdigest(),
                'Metadata':{'sha256':hashlib.sha256(body).hexdigest()}}
    def get_object(self,*,Bucket,Key,IfMatch=None):
        head=self.head_object(Bucket=Bucket,Key=Key)
        if IfMatch is not None:assert IfMatch==head['ETag']
        self.downloaded.append(Key)
        return {**head,'Body':io.BytesIO(self.files[Key])}
    def get_paginator(self,name):
        assert name=='list_objects_v2'
        return SimpleNamespace(paginate=lambda **kw:[{'Contents':[{'Key':k,'Size':len(v)} for k,v in self.files.items()]}])


class BootstrapTests(unittest.TestCase):
    def test_active_tree_neural_and_preprocessor_dependencies(self):
        rows=[('file:///app/models/tree.pkl',{}),('models/lstm.keras',
              {'model_family':'neural_sequence','preprocessor_file':'/app/models/pre.pkl'})]
        self.assertEqual(restore.keys_for_rows(rows),{'models/tree.pkl','models/lstm.keras','models/pre.pkl'})
        for reference in ('/etc/passwd','models/../escape','models//bad','models\\bad',None):
            with self.subTest(reference=reference),self.assertRaises(ValueError):restore.model_key(reference)
        with self.assertRaises(ValueError):restore.keys_for_rows([('models/gru.keras',{'model_family':'neural_sequence'})])

    def test_history_larger_than_budget_does_not_block_required_artifacts(self):
        client=Mock();client.get_paginator.return_value.paginate.return_value=[{'Contents':[
            {'Key':'models/history.pkl','Size':20*1024**3},
            {'Key':'models/active.keras','Size':4},{'Key':'models/pre.pkl','Size':3},
            {'Key':'models/shap.json','Size':2},{'Key':'data/old.csv','Size':1},
            {'Key':'results/old.bin','Size':1}]}]
        entries,report=restore.inventory(client,'synthetic',max_bytes=11,
            required_keys={'models/active.keras','models/pre.pkl'})
        self.assertEqual((report['files'],report['bytes']),(5,11))
        self.assertEqual(report['skipped_model_bytes'],20*1024**3)
        self.assertTrue(report['complete']);self.assertEqual(report['mode'],'active')
        self.assertNotIn('models/history.pkl',dict(entries));client.get_object.assert_not_called()

    def test_missing_required_artifact_and_internal_metadata_fail_closed(self):
        for files,keys in [({}, {'models/missing.pkl'}),({'models/.traffic-cache/index.json':b'{}'},set())]:
            client=Objects(files)
            with self.subTest(files=files),self.assertRaises(RuntimeError):
                restore.inventory(client,'synthetic',required_keys=keys)
            self.assertFalse(client.downloaded)

    def test_real_init_selection_retains_metadata_and_records_durable_models(self):
        files={'models/tree.pkl':b'active','models/history.pkl':b'history',
               'models/shap.json':b'{}','data/source.csv':b'csv'}
        client=Objects(files)
        uploader=SimpleNamespace(get_upload_client=lambda:client)
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{
                'TRAFFIC_CACHE_RESTORE_MODE':'active','TRAFFIC_API_RUNTIME_BUCKET':'synthetic'}),\
             patch.object(restore,'bootstrap_keys',return_value={'models/tree.pkl'}),\
             patch.dict(sys.modules,{'backend.app.services.region_cleanup_uploader':uploader}),redirect_stdout(io.StringIO()) as log:
            restore.restore(Path(directory))
            self.assertFalse((Path(directory)/'models/history.pkl').exists())
            self.assertTrue((Path(directory)/'models/shap.json').is_file())
            cache=ArtifactCache(Path(directory)/'models')
            with cache.hold():self.assertTrue(cache.clean(Path(directory)/'models/tree.pkl'))
            self.assertEqual(json.loads(log.getvalue())['restore_inventory']['skipped_model_files'],1)

    def test_bootstrap_database_transaction_is_read_only(self):
        db=Mock();db.bind.dialect.name='postgresql'
        db.execute.return_value.all.return_value=[('models/tree.pkl',{})]
        factory=Mock();factory.return_value.__enter__=Mock(return_value=db)
        factory.return_value.__exit__=Mock(return_value=False)
        with patch('backend.app.db.session.SessionLocal',factory):
            self.assertEqual(restore.bootstrap_keys(),{'models/tree.pkl'})
        self.assertEqual(str(db.execute.call_args_list[0].args[0]),'SET TRANSACTION READ ONLY')
        db.commit.assert_not_called()


class DiskCacheTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)/'models';self.root.mkdir()
        self.cache=ArtifactCache(self.root,budget=262144,reserve=0)
        self.objects=Objects({'models/'+n:b*80000 for n,b in [('a.pkl',b'a'),('b.pkl',b'b'),('c.pkl',b'c')]})
    def get(self,name,pinned=()):return self.cache.ensure(name,client=self.objects,bucket='synthetic',pinned=pinned)

    def test_lru_eviction_only_removes_clean_local_history(self):
        self.get('a.pkl');self.get('b.pkl');self.get('c.pkl',pinned={'b.pkl'})
        self.assertFalse((self.root/'a.pkl').exists())
        self.assertTrue((self.root/'b.pkl').exists());self.assertTrue((self.root/'c.pkl').exists())
        self.assertIn('models/a.pkl',self.objects.files)
        self.assertLessEqual(self.cache.usage(),self.cache.budget)

    def test_active_and_dirty_files_are_preserved_on_overflow(self):
        self.get('a.pkl');self.get('b.pkl')
        for dirty in (False,True):
            if dirty:(self.root/'a.pkl').write_bytes(b'new-unacknowledged-content'*4000)
            pinned={'b.pkl'} if dirty else {'a.pkl','b.pkl'}
            with self.subTest(dirty=dirty),self.assertRaisesRegex(RuntimeError,'disk budget'):
                self.get('c.pkl',pinned)
            self.assertTrue((self.root/'a.pkl').is_file());self.assertTrue((self.root/'b.pkl').is_file())
            self.assertNotIn('models/c.pkl',self.objects.downloaded)

    def test_remote_overwrite_does_not_authorize_local_eviction(self):
        self.get('a.pkl');self.get('b.pkl');self.objects.files['models/a.pkl']=b'overwritten'
        with self.assertRaises(RuntimeError):self.get('c.pkl',{'b.pkl'})
        self.assertEqual((self.root/'a.pkl').read_bytes(),b'a'*80000)

    def test_provider_outage_never_authorizes_eviction(self):
        self.get('a.pkl');self.get('b.pkl')
        original=self.objects.head_object
        def unavailable(**kw):
            if kw['Key']=='models/a.pkl':raise TimeoutError('synthetic outage')
            return original(**kw)
        with patch.object(self.objects,'head_object',side_effect=unavailable),self.assertRaises(TimeoutError):
            self.get('c.pkl',{'b.pkl'})
        self.assertTrue((self.root/'a.pkl').is_file());self.assertNotIn('models/c.pkl',self.objects.downloaded)

    def test_dirty_content_with_preserved_fingerprint_is_not_evicted(self):
        self.get('a.pkl');self.get('b.pkl');(self.root/'a.pkl').write_bytes(b'd'*80000)
        with self.cache.hold():
            records=self.cache.records()
            from backend.app.services.artifact_cache import fingerprint
            records['a.pkl']['fingerprint']=fingerprint(self.root/'a.pkl')
            self.cache.write(records)
        with self.assertRaises(RuntimeError):self.get('c.pkl',{'b.pkl'})
        self.assertEqual((self.root/'a.pkl').read_bytes(),b'd'*80000)

    def test_optional_recency_write_failure_does_not_break_cached_reads(self):
        self.get('a.pkl')
        with patch('backend.app.services.artifact_cache.time.time_ns',return_value=10**30),\
             patch.object(self.cache,'write',side_effect=OSError('synthetic full disk')):
            self.assertEqual(self.get('a.pkl').read_bytes(),b'a'*80000)
        self.assertEqual(self.objects.downloaded,['models/a.pkl'])

    def test_incomplete_or_corrupt_download_is_not_published(self):
        original=self.objects.get_object
        for body in (b'a',b'z'*80000,b'a'*80001):
            def corrupt(**kw):return {**original(**kw),'Body':io.BytesIO(body)}
            with self.subTest(size=len(body)),patch.object(self.objects,'get_object',side_effect=corrupt),self.assertRaises(RuntimeError):
                self.get('a.pkl')
            self.assertFalse((self.root/'a.pkl').exists())
            self.assertFalse(list(self.root.glob('*.tmp')))

    def test_symlink_and_traversal_paths_are_rejected(self):
        for name in ('../outside.pkl','/outside.pkl','.traffic-cache/index.json','a//b.pkl','a\\b.pkl'):
            with self.subTest(name=name),self.assertRaises(ValueError):self.get(name)
        self.assertFalse(self.objects.downloaded)

    def test_cross_process_lock_keeps_in_use_files_stable(self):
        self.get('a.pkl')
        code="from backend.app.services.artifact_cache import ArtifactCache;import sys\nc=ArtifactCache(sys.argv[1])\ntry:\n with c.hold(timeout=.1):print('acquired')\nexcept TimeoutError:print('blocked')"
        with self.cache.hold():
            result=subprocess.run([sys.executable,'-B','-c',code,str(self.root)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'blocked')
        self.assertTrue((self.root/'a.pkl').is_file())

    def test_tree_neural_loaders_and_preprocessor_restore_on_demand(self):
        client=Objects({'models/'+name:b'bytes' for name in ('rf.pkl','xgb.pkl','lgb.pkl','lstm.keras','gru.keras','pre.pkl')})
        with patch.dict(os.environ,{'TRAFFIC_ARTIFACT_CACHE_BYTES':str(restore.MAX_BYTES),'TRAFFIC_API_RUNTIME_BUCKET':'synthetic'}),\
             patch('backend.app.services.serving_runtime.bootstrap_keys',return_value=set()),\
             patch('backend.app.services.region_cleanup.database_barrier',side_effect=lambda *_:nullcontext()):
            runtime=PredictionRuntime(ApplicationError,lambda path:Path(path).read_bytes(),root=self.root,client=client)
            predictions=SimpleNamespace(_resolve_model_path=lambda p:p,
                _load_keras_model_from_path=lambda p:Path(p).read_bytes(),
                _load_neural_preprocessors=lambda p:Path(p).read_bytes())
            install_neural_restore(predictions,runtime)
            for name in ('rf.pkl','xgb.pkl','lgb.pkl'):
                self.assertEqual(runtime.load_model(str(self.root/name)),b'bytes')
            for name in ('lstm.keras','gru.keras'):
                self.assertEqual(predictions._load_keras_model_from_path(str(self.root/name)),b'bytes')
            self.assertEqual(predictions._load_neural_preprocessors(str(self.root/'pre.pkl')),b'bytes')
            self.assertEqual(len(client.downloaded),6)


class StorageCapacityTests(unittest.TestCase):
    def test_manifest_restore_and_runtime_budgets_agree(self):
        path=Path(__file__).resolve().parents[1]/'deployment/rke2/roles/api.json'
        pod=json.loads(path.read_text())['spec']['template']['spec']
        self.assertEqual(next(v for v in pod['volumes'] if v['name']=='artifact-cache')['emptyDir']['sizeLimit'],'6Gi')
        self.assertEqual(restore.MAX_BYTES,restore.CACHE_VOLUME_BYTES*4//5)
        for container in [pod['containers'][0],pod['initContainers'][-1]]:
            env={e['name']:e.get('value') for e in container['env']}
            self.assertEqual(int(env['TRAFFIC_ARTIFACT_CACHE_BYTES']),restore.MAX_BYTES)
        self.assertEqual(requests(pod)['ephemeral-storage'],5.25*1024**3)
        self.assertEqual(staging_disk(pod),6.5*1024**3)

    def test_free_disk_above_old_threshold_but_below_staging_plus_reserve_fails(self):
        pod={'containers':[],'volumes':[{'emptyDir':{'sizeLimit':'6Gi'}}]}
        host={'cpus':4,'memory_available':8*1024**3,'disk_available':16*1024**3}
        with self.assertRaisesRegex(RuntimeError,'disk headroom'):verify([],[('node',pod)],{'node':host})
        host['disk_available']=20*1024**3
        self.assertEqual(verify([],[('node',pod)],{'node':host})['nodes']['node']['additional_disk_budget'],6.5*1024**3)


if __name__=='__main__':unittest.main()
