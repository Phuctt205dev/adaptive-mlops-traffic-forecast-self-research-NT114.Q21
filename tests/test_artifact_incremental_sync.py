from contextlib import nullcontext
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from backend.app.services import region_cleanup_uploader as uploader
from backend.app.services import artifact_sync_control as sync
from backend.app.services.artifact_cache import ArtifactCache


class IncrementalArtifactSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.file = self.root / 'models/model.pkl'; self.file.parent.mkdir(); self.file.write_bytes(b'first')
        self.clock = 0
        self.state = uploader.UploadState(reconcile_seconds=3600, clock=lambda: self.clock)
        self.client = MagicMock()
        session = MagicMock(); session.__enter__.return_value.scalars.return_value = []
        for target, options in (
            ('get_upload_client', {'return_value': self.client}),
            ('database_barrier', {'side_effect': lambda *_: nullcontext(MagicMock())}),
            ('SessionLocal', {'return_value': session}),
            ('pending_cleanup', {'return_value': None}),
        ):
            replacement = patch.object(uploader, target, **options); replacement.start(); self.addCleanup(replacement.stop)
        replacement = patch.dict(uploader.os.environ, {'TRAFFIC_API_RUNTIME_BUCKET': 'synthetic'})
        replacement.start(); self.addCleanup(replacement.stop)

    def cycle(self):
        return uploader.upload_cycle(self.root, state=self.state)

    def test_unchanged_file_skips_transfer_and_database_work_until_reconciliation(self):
        self.assertEqual(self.cycle(), 1)
        uploader.database_barrier.reset_mock()
        self.assertEqual(self.cycle(), 0)
        uploader.database_barrier.assert_not_called()
        self.assertEqual(self.state.unchanged, 1)
        self.clock = 3601
        self.assertEqual(self.cycle(), 1)
        self.assertEqual(self.client.upload_file.call_count, 2)

    def test_failed_transfer_is_retried_and_never_acknowledged(self):
        self.client.upload_file.side_effect = RuntimeError('synthetic upload failure')
        with self.assertRaises(RuntimeError): self.cycle()
        self.assertEqual(self.state.acknowledged, {})
        self.client.upload_file.side_effect = None
        self.assertEqual(self.cycle(), 1)
        self.assertEqual(self.cycle(), 0)

    def test_file_changed_during_transfer_is_uploaded_again(self):
        self.client.upload_file.side_effect = lambda *args, **kwargs: self.file.write_bytes(b'changed while uploading')
        self.assertEqual(self.cycle(), 1)
        self.assertEqual(self.state.acknowledged, {})
        self.client.upload_file.side_effect = None
        self.assertEqual(self.cycle(), 1)

    def test_atomic_replacement_is_detected_and_temporary_files_are_skipped(self):
        self.cycle()
        temporary = self.file.with_suffix('.tmp'); temporary.write_bytes(b'replacement')
        self.assertEqual(self.cycle(), 0)
        temporary.replace(self.file)
        self.assertEqual(self.cycle(), 1)

    def test_cleanup_fence_still_prevents_upload_during_reconciliation(self):
        self.cycle(); self.clock = 3601
        with patch.object(uploader, 'owns_key', return_value=True):
            self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.client.upload_file.call_count, 1)
        self.assertEqual(self.state.acknowledged, {})


class ManagedDiskSyncTests(unittest.TestCase):
    def setUp(self):
        IncrementalArtifactSyncTests.setUp(self)
        replacement=patch.dict(uploader.os.environ,{'TRAFFIC_ARTIFACT_CACHE_BYTES':'262144'})
        replacement.start();self.addCleanup(replacement.stop)
        self.remote={}
        def upload(filename,bucket,key,**kwargs):
            body=Path(filename).read_bytes()
            self.remote[key]={'ContentLength':len(body),'ETag':hashlib.md5(body).hexdigest(),
                              'Metadata':kwargs.get('ExtraArgs',{}).get('Metadata',{})}
        self.client.upload_file.side_effect=upload
        self.upload=upload
        self.client.head_object.side_effect=lambda **kw:self.remote[kw['Key']]

    def cycle(self):return uploader.upload_cycle(self.root,state=self.state)

    def test_uploaded_files_become_evictable_only_after_stable_ack(self):
        self.assertEqual(self.cycle(),1)
        cache=ArtifactCache(self.file.parent,budget=262144,reserve=0)
        with cache.hold():self.assertTrue(cache.clean(self.file))
        self.assertEqual(self.remote['models/model.pkl']['Metadata']['sha256'],hashlib.sha256(b'first').hexdigest())
        self.assertEqual(set(self.state.acknowledged),{'models/model.pkl'})
        self.assertEqual(self.cycle(),0)
        self.assertEqual(self.client.upload_file.call_count,1)

    def test_managed_metadata_is_never_uploaded_or_required_by_flush(self):
        self.cycle()
        proof=sync.flush(self.root,module=uploader,state=self.state)
        self.assertTrue(proof['complete']);self.assertEqual(proof['acknowledged_files'],1)
        self.assertEqual(set(self.remote),{'models/model.pkl'})

    def test_successful_upload_without_matching_remote_checksum_is_not_evictable(self):
        self.client.head_object.side_effect=None
        self.client.head_object.return_value={'ContentLength':5,'ETag':'another-writer','Metadata':{'sha256':'different'}}
        self.cycle()
        cache=ArtifactCache(self.file.parent,budget=262144,reserve=0)
        with cache.hold():self.assertFalse(cache.clean(self.file))

    def test_background_prune_preserves_active_and_acknowledges_new_artifacts(self):
        self.file.write_bytes(b'a'*80000);self.cycle()
        second=self.file.with_name('active.pkl');second.write_bytes(b'b'*80000);self.cycle()
        third=self.file.with_name('new.pkl');third.write_bytes(b'c'*80000)
        with patch.object(uploader,'bootstrap_keys',return_value={'models/active.pkl'}):
            self.assertEqual(uploader.upload_cycle(self.root,state=self.state,prune=True),1)
        self.assertFalse(self.file.exists());self.assertTrue(second.exists());self.assertTrue(third.exists())
        self.assertIn('models/model.pkl',self.remote)
        # Explicit retirement flush does not prune underneath its inventory.
        self.assertTrue(sync.flush(self.root,module=uploader,state=self.state)['complete'])

    def test_failed_or_changed_transfer_never_marks_the_disk_copy_clean(self):
        cache=ArtifactCache(self.file.parent,budget=262144,reserve=0)
        self.client.upload_file.side_effect=RuntimeError('synthetic upload failure')
        with self.assertRaises(RuntimeError):self.cycle()
        self.assertFalse(self.state.acknowledged)
        self.client.upload_file.side_effect=lambda *args,**kw:self.file.write_bytes(b'changed-during-upload')
        self.cycle()
        with cache.hold():self.assertFalse(cache.clean(self.file))
        self.assertFalse(self.state.acknowledged)
        self.client.upload_file.side_effect=self.upload
        self.cycle()
        with cache.hold():self.assertTrue(cache.clean(self.file))

    def test_tombstones_still_remove_acknowledgements_for_clean_cache_entries(self):
        self.cycle();self.clock=3601
        with patch.object(uploader,'owns_key',return_value=True):self.assertEqual(self.cycle(),0)
        self.assertFalse(self.state.acknowledged)

    def test_pending_cleanup_prevents_background_prune(self):
        self.cycle()
        with patch.object(uploader,'pending_cleanup',return_value=object()),\
             patch.object(uploader,'bootstrap_keys') as pins:
            uploader.upload_cycle(self.root,state=self.state,prune=True)
        pins.assert_not_called();self.assertTrue(self.file.exists())


if __name__ == '__main__':
    unittest.main()
