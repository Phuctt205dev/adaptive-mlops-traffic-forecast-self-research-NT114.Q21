from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from backend.app.services import region_cleanup_uploader as uploader


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


if __name__ == '__main__':
    unittest.main()
