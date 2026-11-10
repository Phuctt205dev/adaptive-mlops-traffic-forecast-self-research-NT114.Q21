"""Download a complete CI ZIP and verify it before acquiring CD ownership.

Only GitHub GETs and a fresh local directory are used. No state/cluster writes.
The original ZIP digest, role manifest and image bindings remain mandatory.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import ssl
import stat
import sys
import tempfile
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.cicd.role_manifests import CONTRACT, SLOTS, deployment_id, render
from scripts.cicd.roles_deploy import validate_delivery
from scripts.cicd.roles_release import verify

MAX_ZIP = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_FILES = 4096
CHUNK = 64 * 1024


class PreparationError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__('Release preparation failed: ' + category)


def https_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise PreparationError('unsafe_redirect')
    return parsed


class ArtifactRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        target = https_url(newurl)
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if target.netloc != urlsplit(request.full_url).netloc:
            redirected.remove_header('Authorization')
        return redirected


class Deadline:
    def __init__(self, seconds=300, *, clock=time.monotonic):
        self.clock, self.end = clock, clock() + seconds

    def timeout(self):
        remaining = self.end - self.clock()
        if remaining <= 0:
            raise PreparationError('deadline_exceeded')
        return min(30, remaining)


def transient(error, *, archive=False):
    if isinstance(error, HTTPError):
        return error.code in {408, 429, 500, 502, 503, 504} | ({403} if archive else set())
    return isinstance(error, (URLError, TimeoutError, ConnectionError, http.client.HTTPException)) or (
        isinstance(error, PreparationError) and error.category == 'incomplete_download')


class GitHubArtifactClient:
    def __init__(self, repository, token, *, deadline=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not token:
            raise PreparationError('invalid_repository_or_credential')
        self.repository, self.token = repository, token
        self.deadline = deadline or Deadline()
        context = ssl.create_default_context()
        if os.name == 'nt' and os.environ.get('NODE_EXTRA_CA_CERTS'):
            context.load_verify_locations(cafile=os.environ['NODE_EXTRA_CA_CERTS'])
        self.opener = build_opener(ArtifactRedirect(), HTTPSHandler(context=context))

    def open(self, path):
        request = Request('https://api.github.com/repos/' + self.repository + '/' + path,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'Cache-Control': 'no-cache'})
        return self.opener.open(request, timeout=self.deadline.timeout())

    def get(self, path):
        for attempt in range(3):
            try:
                with self.open(path) as response:
                    chunks, size = [], 0
                    reader = getattr(response, 'read1', response.read)
                    while True:
                        self.deadline.timeout()
                        chunk = reader(CHUNK)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > 2 * 1024 * 1024:
                            raise PreparationError('metadata_budget')
                        chunks.append(chunk)
                    raw = b''.join(chunks)
                self.deadline.timeout()
                return json.loads(raw)
            except Exception as error:
                if not transient(error) or attempt == 2:
                    raise
                time.sleep(min(.5 * (attempt + 1), self.deadline.timeout()))

    def archive(self, artifact_id):
        return self.open('actions/artifacts/' + str(artifact_id) + '/zip')


def select_artifact(client, run_id, commit, *, current_run=False):
    if not re.fullmatch('[1-9][0-9]*', str(run_id)) or not re.fullmatch('[0-9a-f]{40}', commit):
        raise PreparationError('invalid_source_identity')
    run = client.get('actions/runs/' + str(run_id))
    terminal = run.get('status') == 'completed' and run.get('conclusion') == 'success'
    if current_run:
        from scripts.cicd.github_gate import same_run
        terminal = same_run(run, run_id, commit, int(os.environ.get('GITHUB_RUN_ATTEMPT', '0')))
    if (str(run.get('id')) != str(run_id) or run.get('head_sha') != commit
            or not terminal
            or run.get('event') != 'push'
            or (run.get('head_repository') or {}).get('full_name', '').lower() != client.repository.lower()):
        raise PreparationError('source_run_mismatch')
    found = []
    for page in range(1, 21):
        data = client.get(f'actions/runs/{run_id}/artifacts?per_page=100&page={page}')
        entries = data.get('artifacts')
        if not isinstance(entries, list) or len(entries) > 100:
            raise PreparationError('invalid_artifact_listing')
        found.extend(a for a in entries if a.get('name') == 'backend-release-' + commit and a.get('expired') is False)
        if len(entries) < 100:
            break
    else:
        raise PreparationError('artifact_listing_budget')
    if len(found) != 1:
        raise PreparationError('missing_or_ambiguous_artifact')
    item = found[0]
    identity = item.get('workflow_run') or {}
    if (type(item.get('id')) is not int or item['id'] < 1
            or type(item.get('size_in_bytes')) is not int or not 0 < item['size_in_bytes'] <= MAX_ZIP
            or not isinstance(item.get('digest'), str) or not re.fullmatch('sha256:[0-9a-f]{64}', item['digest'])
            or str(identity.get('id')) != str(run_id) or identity.get('head_sha') != commit):
        raise PreparationError('artifact_identity_or_digest_mismatch')
    return item


def download(client, artifact, directory, *, sleep=time.sleep):
    partial, complete = directory / 'release.zip.part', directory / 'release.zip'
    for attempt in range(3):
        try:
            client.deadline.timeout()
            size, digest = 0, hashlib.sha256()
            with client.archive(artifact['id']) as response, partial.open('xb') as target:
                reader = getattr(response, 'read1', response.read)
                while True:
                    client.deadline.timeout()
                    chunk = reader(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > artifact['size_in_bytes'] or size > MAX_ZIP:
                        raise PreparationError('download_byte_budget')
                    digest.update(chunk)
                    target.write(chunk)
            client.deadline.timeout()
            if size != artifact['size_in_bytes']:
                raise PreparationError('incomplete_download')
            if 'sha256:' + digest.hexdigest() != artifact['digest']:
                raise PreparationError('zip_digest_mismatch')
            partial.replace(complete)
            return complete
        except Exception as error:
            if partial.is_file():
                partial.unlink()  # Only this operation's file, in its private temporary directory.
            if not transient(error, archive=True) or attempt == 2:
                raise
            sleep(min(.5 * (attempt + 1), client.deadline.timeout()))
    raise PreparationError('download_failed')


def archive_files(archive):
    entries, seen, total = [], {}, 0
    for info in archive.infolist():
        name = info.filename.rstrip('/') if info.is_dir() else info.filename
        path = PurePosixPath(name)
        parts = name.split('/')
        if (info.orig_filename != info.filename or not name or path.is_absolute() or any(p in {'', '.', '..'} for p in parts)
                or any(re.search(r'[\x00-\x1f<>:"\\|?*]', p) or p.endswith(('.', ' '))
                       or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p, re.I) for p in parts)
                or info.flag_bits & 1 or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)):
            raise PreparationError('unsafe_zip_entry')
        key = name.casefold()
        if key in seen or any(key.startswith(other + '/') and not is_dir or other.startswith(key + '/') and not info.is_dir()
                              for other, is_dir in seen.items()):
            raise PreparationError('colliding_zip_entries')
        seen[key] = info.is_dir()
        if len(seen) > MAX_FILES or info.file_size < 0:
            raise PreparationError('archive_file_budget')
        total += info.file_size
        if total > MAX_EXPANDED:
            raise PreparationError('archive_byte_budget')
        if not info.is_dir():
            entries.append(info)
    return entries


def extract_and_verify(zip_path, stage, commit, deadline):
    package = stage / 'package'
    package.mkdir()
    binding_path = stage / 'delivery.json'
    with zipfile.ZipFile(zip_path) as archive:
        entries = archive_files(archive)
        for info in entries:
            target = binding_path if info.filename == 'delivery.json' else package / info.filename
            if not target.resolve().is_relative_to(stage.resolve()):
                raise PreparationError('unsafe_zip_entry')
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open('xb') as destination:
                while True:
                    deadline.timeout()
                    chunk = source.read(CHUNK)  # ZipFile also validates each entry's CRC.
                    if not chunk:
                        break
                    destination.write(chunk)
    deadline.timeout()
    release = verify(package, expected_commit=commit)
    bound = json.loads(binding_path.read_bytes())
    images = validate_delivery(release, bound)
    wanted = {role: deployment_id(render(role, slots[0], images, directory=package)) for role, slots in SLOTS.items()}
    if bound.get('runtime_contract') != CONTRACT or bound.get('deployments') != wanted:
        raise PreparationError('deployment_binding_mismatch')
    deadline.timeout()
    binding_path.replace(package / 'delivery.json')
    return package, release


def publish(package, destination, deadline, *, sleep=time.sleep):
    # Windows can briefly deny a directory rename while another local process
    # has a handle open. Retry only this local operation, never the download or
    # a deployment, and never replace a destination created by another owner.
    for attempt in range(6):
        deadline.timeout()
        if destination.exists():
            raise PreparationError('destination_not_fresh')
        try:
            package.rename(destination)
            return
        except PermissionError as error:
            if getattr(error, 'winerror', None) not in {5, 32, 33} or attempt == 5:
                raise
            sleep(min(.1 * 2 ** attempt, deadline.timeout()))


def prepare(client, run_id, commit, destination, *, current_run=False):
    destination = Path(destination).absolute()
    if destination.exists() or any(p.is_symlink() for p in (destination, *destination.parents)):
        raise PreparationError('destination_not_fresh')
    destination.parent.mkdir(parents=True, exist_ok=True)
    artifact = select_artifact(client, run_id, commit, current_run=current_run)
    with tempfile.TemporaryDirectory(prefix='traffic-package-', dir=destination.parent) as temporary:
        stage = Path(temporary).resolve()
        if not stage.is_relative_to(destination.parent.resolve()) or stage == destination.parent.resolve():
            raise PreparationError('unsafe_staging_directory')
        zip_path = download(client, artifact, stage)
        package, release = extract_and_verify(zip_path, stage, commit, client.deadline)
        publish(package, destination, client.deadline)  # Same filesystem, only after verification.
    return {'schema': 'traffic-artifact-preparation/v1', 'prepared': True, 'commit': commit,
            'ci_run_id': str(run_id), 'artifact_id': artifact['id'], 'zip_digest': artifact['digest'],
            'zip_bytes': artifact['size_in_bytes'], 'package_files': len(release['files']) + 1,
            'release_id': release['release_id'], 'cluster_mutated': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--current-run', action='store_true')
    args = parser.parse_args()
    result = prepare(GitHubArtifactClient(os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN']),
                     args.run_id, args.commit, args.destination, current_run=args.current_run)
    print(json.dumps(result))
    if os.environ.get('GITHUB_OUTPUT'):
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
            stream.write('artifact_directory=' + str(Path(args.destination).absolute()) + '\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
            stream.write('### Release package verified before CD claim\n\n'
                         f'- CI run: `{result["ci_run_id"]}`\n- ZIP SHA256 verified\n'
                         f'- Package files verified: {result["package_files"]}\n')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'preparation_failed': True, 'cluster_mutated': False,
                          'category': error.category if isinstance(error, PreparationError) else 'package_preparation_error',
                          'error_type': type(error).__name__,
                          'http_status': error.code if isinstance(error, HTTPError) else None,
                          'os_error': getattr(error, 'winerror', None),
                          'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno}
                                     for frame in traceback.extract_tb(error.__traceback__)[-8:]]}))
        raise SystemExit(1)
