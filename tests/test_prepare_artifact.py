"""Real ZIP/manifest checks and simulated interrupted GETs; no cluster writes."""
import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request
from urllib.error import HTTPError
import zipfile

from scripts.cicd import prepare_artifact as prepare
from scripts.cicd.role_manifests import CONTRACT, SLOTS, deployment_id, render
from scripts.cicd.roles_release import build

ROOT = Path(__file__).resolve().parents[1]
COMMIT = 'a' * 40


class FakeClient:
    repository = 'owner/repo'

    def __init__(self, blob):
        self.blob = blob
        self.deadline = prepare.Deadline()
        self.calls = 0
        self.run = {'id': 29, 'head_sha': COMMIT, 'event': 'push', 'status': 'completed', 'conclusion': 'success',
                    'head_repository': {'full_name': self.repository}}
        self.item = {'id': 123, 'name': 'backend-release-' + COMMIT, 'expired': False,
                     'size_in_bytes': len(blob), 'digest': 'sha256:' + hashlib.sha256(blob).hexdigest(),
                     'workflow_run': {'id': 29, 'head_sha': COMMIT}}
        self.items = [self.item]

    def get(self, path):
        return {'artifacts': self.items} if '/artifacts?' in path else self.run

    def archive(self, identity):
        self.calls += 1
        assert identity == self.item['id']
        return io.BytesIO(self.blob)


class ArtifactPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.shared.cleanup)
        package = Path(cls.shared.name) / 'release'
        release = build(ROOT, package, COMMIT)
        images = {role: {'role': role, 'source_id': meta['source_id'], 'commit': COMMIT,
                         'image': 'ghcr.io/example/backend@sha256:' + hashlib.sha256(role.encode()).hexdigest()}
                  for role, meta in release['roles'].items()}
        bound = {'schema': 'traffic-delivery/v2', 'commit': COMMIT, 'release_id': release['release_id'],
                 'image': images['api']['image'], 'images': images, 'runtime_contract': CONTRACT,
                 'deployments': {role: deployment_id(render(role, slots[0], images, directory=package))
                                 for role, slots in SLOTS.items()}}
        cls.files = {path.relative_to(package).as_posix(): path.read_bytes() for path in package.rglob('*') if path.is_file()}
        cls.files['delivery.json'] = json.dumps(bound).encode()
        cls.blob = cls.zip(cls.files)

    @staticmethod
    def zip(files):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, body in files.items():
                archive.writestr(name, body)
        return output.getvalue()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / 'prepared'

    def assert_fails_without_exposing_package(self, client, commit=COMMIT):
        with self.assertRaises(Exception) as caught:
            prepare.prepare(client, 29, commit, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.iterdir()), [])
        return caught.exception

    def test_complete_package_is_published_only_after_original_manifest_and_bindings_pass(self):
        client = FakeClient(self.blob)
        def archive(identity):
            self.assertFalse(self.destination.exists())
            return io.BytesIO(self.blob)
        client.archive = archive
        result = prepare.prepare(client, 29, COMMIT, self.destination)
        self.assertTrue(result['prepared'])
        self.assertFalse(result['cluster_mutated'])
        actual = {p.relative_to(self.destination).as_posix(): p.read_bytes()
                  for p in self.destination.rglob('*') if p.is_file()}
        self.assertEqual(actual, self.files)
        self.assertEqual(list(self.root.iterdir()), [self.destination])
        self.assertEqual(result['package_files'], len(self.files) - 1)

    def test_truncated_download_retries_with_a_fresh_get_then_succeeds(self):
        client = FakeClient(self.blob)
        replies = [io.BytesIO(self.blob[:100]), io.BytesIO(self.blob)]
        client.archive = Mock(side_effect=replies)
        with patch('scripts.cicd.prepare_artifact.time.sleep'):
            result = prepare.prepare(client, 29, COMMIT, self.destination)
        self.assertTrue(result['prepared'])
        self.assertEqual(client.archive.call_count, 2)

    def test_interrupted_response_never_exposes_partial_files_and_retry_is_bounded(self):
        client = FakeClient(self.blob)
        class Interrupted(io.BytesIO):
            def read1(self, size):
                raise http.client.IncompleteRead(b'PRIVATE_SIGNED_URL')
        client.archive = Mock(side_effect=lambda _: Interrupted(self.blob))
        with patch('scripts.cicd.prepare_artifact.time.sleep'):
            self.assert_fails_without_exposing_package(client)
        self.assertEqual(client.archive.call_count, 3)

    def test_checksum_mismatch_is_fatal_and_never_rewrites_the_expected_digest(self):
        client = FakeClient(self.blob)
        client.item['digest'] = 'sha256:' + '0' * 64
        error = self.assert_fails_without_exposing_package(client)
        self.assertEqual(error.category, 'zip_digest_mismatch')
        self.assertEqual(client.calls, 1)
        self.assertEqual(client.item['digest'], 'sha256:' + '0' * 64)

    def test_missing_duplicate_expired_or_wrong_source_artifact_is_rejected_before_get(self):
        for broken in ('missing', 'duplicate', 'expired', 'wrong_run', 'wrong_commit', 'no_digest'):
            with self.subTest(broken=broken):
                client = FakeClient(self.blob)
                if broken == 'missing': client.items = []
                if broken == 'duplicate': client.items.append(copy.deepcopy(client.item))
                if broken == 'expired': client.item['expired'] = True
                if broken == 'wrong_run': client.item['workflow_run']['id'] = 28
                if broken == 'wrong_commit': client.item['workflow_run']['head_sha'] = 'b' * 40
                if broken == 'no_digest': client.item.pop('digest')
                self.assert_fails_without_exposing_package(client)
                self.assertEqual(client.calls, 0)

    def test_failed_run_or_wrong_requested_commit_never_downloads(self):
        for changes in ({'conclusion': 'failure'}, {'event': 'pull_request'}, {'head_sha': 'b' * 40}):
            client = FakeClient(self.blob)
            client.run.update(changes)
            self.assert_fails_without_exposing_package(client)
            self.assertEqual(client.calls, 0)

    def test_existing_destination_is_preserved(self):
        self.destination.mkdir()
        preserved = self.destination / 'preserve.txt'
        preserved.write_bytes(b'original')
        client = FakeClient(self.blob)
        with self.assertRaises(prepare.PreparationError):
            prepare.prepare(client, 29, COMMIT, self.destination)
        self.assertEqual(preserved.read_bytes(), b'original')
        self.assertEqual(client.calls, 0)

    def test_temporary_windows_rename_denial_retries_only_the_local_publish(self):
        package = self.root / 'package'
        package.mkdir()
        (package / 'verified.txt').write_bytes(b'verified')
        original, attempts, sleeps = Path.rename, [0], []
        error = PermissionError('Synthetic Windows handle contention')
        error.winerror = 5
        def rename(path, destination):
            attempts[0] += 1
            if attempts[0] < 3:
                raise error
            return original(path, destination)
        with patch.object(Path, 'rename', autospec=True, side_effect=rename):
            prepare.publish(package, self.destination, prepare.Deadline(), sleep=sleeps.append)
        self.assertEqual(attempts[0], 3)
        self.assertEqual(sleeps, [.1, .2])
        self.assertEqual((self.destination / 'verified.txt').read_bytes(), b'verified')

    def test_permanent_rename_denial_is_bounded_and_never_exposes_destination(self):
        for winerror, count in ((5, 6), (None, 1)):
            package = self.root / ('package-' + str(winerror))
            package.mkdir()
            error = PermissionError('Synthetic permanent denial')
            if winerror is not None: error.winerror = winerror
            with patch.object(Path, 'rename', side_effect=error) as rename:
                with self.assertRaises(PermissionError):
                    prepare.publish(package, self.destination, prepare.Deadline(), sleep=lambda _: None)
            self.assertEqual(rename.call_count, count)
            self.assertFalse(self.destination.exists())
            self.assertTrue(package.is_dir())

    def test_concurrent_destination_during_rename_retry_is_preserved(self):
        package = self.root / 'package'
        package.mkdir()
        error = PermissionError('Synthetic Windows handle contention')
        error.winerror = 32
        def concurrent_owner(_):
            self.destination.mkdir()
            (self.destination / 'preserve').write_bytes(b'other owner')
        with patch.object(Path, 'rename', side_effect=error) as rename:
            with self.assertRaises(prepare.PreparationError):
                prepare.publish(package, self.destination, prepare.Deadline(), sleep=concurrent_owner)
        rename.assert_called_once()
        self.assertEqual((self.destination / 'preserve').read_bytes(), b'other owner')

    def test_valid_zip_with_missing_altered_extra_file_or_invalid_binding_still_fails(self):
        for broken in ('missing', 'altered', 'extra', 'binding', 'deployment'):
            with self.subTest(broken=broken):
                files = dict(self.files)
                name = 'roles/api/backend/app/core/config.py'
                if broken == 'missing': files.pop(name)
                if broken == 'altered': files[name] += b'\n# tampered'
                if broken == 'extra': files['unreviewed.txt'] = b'extra'
                if broken in {'binding', 'deployment'}:
                    bound = json.loads(files['delivery.json'])
                    if broken == 'binding': bound['images']['neural']['source_id'] = '0' * 64
                    else: bound['deployments']['api'] = '0' * 64
                    files['delivery.json'] = json.dumps(bound).encode()
                self.assert_fails_without_exposing_package(FakeClient(self.zip(files)))

    def test_invalid_zip_even_with_matching_download_digest_never_publishes(self):
        self.assert_fails_without_exposing_package(FakeClient(b'not a ZIP'))

    def test_crc_failure_is_rejected_even_when_the_zip_digest_matches_metadata(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('payload.txt', b'SYNTHETIC_CRC_PAYLOAD')
        damaged = output.getvalue().replace(b'SYNTHETIC_CRC_PAYLOAD', b'XYNTHETIC_CRC_PAYLOAD')
        error = self.assert_fails_without_exposing_package(FakeClient(damaged))
        self.assertIsInstance(error, zipfile.BadZipFile)

    def test_expired_signed_url_refreshes_get_but_auth_failure_is_fatal(self):
        for status, expected_calls in ((401, 1), (403, 2)):
            with self.subTest(status=status):
                client = FakeClient(self.blob)
                client.archive = Mock(side_effect=[HTTPError('https://private?sig=secret', status, 'private', {}, None),
                                                   io.BytesIO(self.blob)])
                with patch('scripts.cicd.prepare_artifact.time.sleep'):
                    if status == 403:
                        prepare.prepare(client, 29, COMMIT, self.destination)
                    else:
                        self.assert_fails_without_exposing_package(client)
                self.assertEqual(client.archive.call_count, expected_calls)

    def test_metadata_retries_only_transient_get_failures(self):
        client = prepare.GitHubArtifactClient('owner/repo', 'SYNTHETIC_TOKEN')
        client.opener = Mock()
        client.opener.open.side_effect = [HTTPError('https://api.github.com/private', 503, 'private', {}, None),
                                         io.BytesIO(b'{"ok":true}')]
        with patch('scripts.cicd.prepare_artifact.time.sleep'):
            self.assertEqual(client.get('actions/runs/29'), {'ok': True})
        self.assertEqual(client.opener.open.call_count, 2)
        for call in client.opener.open.call_args_list:
            self.assertEqual(call.args[0].get_method(), 'GET')

    def test_cli_failure_is_nonzero_and_omits_credential_and_signed_url(self):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/cicd/prepare_artifact.py'),
                                 '--run-id', '29', '--commit', COMMIT, '--destination', str(self.destination)],
                                env={**os.environ, 'GITHUB_REPOSITORY': 'invalid/repo/extra',
                                     'GH_TOKEN': 'SYNTHETIC_SECRET', 'GITHUB_OUTPUT': str(self.root / 'output')},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(json.loads(result.stdout)['preparation_failed'])
        self.assertNotIn('SYNTHETIC_SECRET', result.stdout + result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / 'output').exists())

    def test_unsafe_windows_paths_collisions_and_symlinks_are_rejected(self):
        for name in ('../escape.txt', '/escape.txt', 'C:/escape.txt', 'folder\\escape.txt',
                     'NUL.txt', 'name.', 'name ', 'file:stream', 'with?wildcard'):
            blob = self.zip({name: b'bad'})
            if '\\' in name:  # ZipInfo normalizes Windows separators while writing.
                blob = blob.replace(name.replace('\\', '/').encode(), name.encode())
            with self.subTest(name=name), zipfile.ZipFile(io.BytesIO(blob)) as archive:
                with self.assertRaises(prepare.PreparationError): prepare.archive_files(archive)
        for files in ({'File': b'a', 'file': b'b'}, {'folder': b'a', 'folder/child': b'b'}):
            with zipfile.ZipFile(io.BytesIO(self.zip(files))) as archive:
                with self.assertRaises(prepare.PreparationError): prepare.archive_files(archive)
        entry = zipfile.ZipInfo('link')
        entry.create_system, entry.external_attr = 3, 0o120777 << 16
        with zipfile.ZipFile(io.BytesIO(self.zip({entry: b'../target'}))) as archive:
            with self.assertRaises(prepare.PreparationError): prepare.archive_files(archive)

    def test_archive_size_and_download_byte_budgets_are_enforced(self):
        client = FakeClient(self.blob)
        client.item['size_in_bytes'] = 10
        self.assertEqual(self.assert_fails_without_exposing_package(client).category, 'download_byte_budget')
        with patch.object(prepare, 'MAX_EXPANDED', 1), zipfile.ZipFile(io.BytesIO(self.zip({'large': b'too big'}))) as archive:
            with self.assertRaises(prepare.PreparationError): prepare.archive_files(archive)

    def test_deadline_is_shared_across_retries(self):
        elapsed = [0]
        client = FakeClient(self.blob)
        client.deadline = prepare.Deadline(5, clock=lambda: elapsed[0])
        def stalled(identity):
            elapsed[0] += 5
            raise TimeoutError()
        client.archive = Mock(side_effect=stalled)
        error = self.assert_fails_without_exposing_package(client)
        self.assertEqual(error.category, 'deadline_exceeded')
        self.assertEqual(client.archive.call_count, 1)

    def test_cross_host_redirect_strips_token_and_insecure_or_userinfo_urls_fail(self):
        request = Request('https://api.github.com/artifact', headers={'Authorization': 'Bearer SYNTHETIC_TOKEN'})
        redirect = prepare.ArtifactRedirect()
        target = redirect.redirect_request(request, None, 302, 'Found', {}, 'https://blob.example/file?sig=private')
        self.assertIsNone(target.get_header('Authorization'))
        same = redirect.redirect_request(request, None, 302, 'Found', {}, 'https://api.github.com/other')
        self.assertEqual(same.get_header('Authorization'), 'Bearer SYNTHETIC_TOKEN')
        for url in ('http://blob.example/file', 'https://user:pass@blob.example/file'):
            with self.assertRaises(prepare.PreparationError):
                redirect.redirect_request(request, None, 302, 'Found', {}, url)

    def test_workflow_requires_preparation_before_claim_and_delivers_the_verified_directory(self):
        workflow = (ROOT / '.github/workflows/backend-deploy.yml').read_text(encoding='utf-8')
        deploy = workflow.split('\n  deploy:\n')[1].split('\n  record-result:\n')[0]
        self.assertLess(deploy.index('id: package'), deploy.index('id: claim'))
        package = deploy.split('id: package')[1].split('\n      - name:')[0]
        self.assertIn('prepare_artifact.py', package)
        self.assertIn('$LASTEXITCODE', package)
        self.assertNotIn('continue-on-error', package)
        self.assertIn('ARTIFACT_DIRECTORY: ${{ steps.package.outputs.artifact_directory }}', deploy)
        self.assertIn('-ArtifactDirectory $env:ARTIFACT_DIRECTORY', deploy)
        self.assertNotIn('download-artifact@', deploy)


if __name__ == '__main__':
    unittest.main()
