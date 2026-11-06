"""Resolve public immutable role images; never print registry bearer tokens."""
import argparse
import hashlib
import json
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def lookup(image, role, source_id):
    if not re.fullmatch(r'ghcr\.io/[a-z0-9._/-]+', image) or role not in {'api','tree','neural','sync'} or not re.fullmatch('[0-9a-f]{64}', source_id):
        raise ValueError('Invalid role registry reference')
    repository = image.removeprefix('ghcr.io/')
    with urlopen('https://ghcr.io/token?service=ghcr.io&scope=repository:' + repository + ':pull', timeout=20) as response:
        token = json.load(response)['token']
    headers = {'Authorization': 'Bearer ' + token,
               'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'}

    def manifest(reference):
        request = Request('https://ghcr.io/v2/' + repository + '/manifests/' + reference, headers=headers)
        with urlopen(request, timeout=20) as response:
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError('Registry metadata exceeds its budget')
            actual = 'sha256:' + hashlib.sha256(data).hexdigest()
            if response.headers.get('Docker-Content-Digest', actual) != actual:
                raise ValueError('Registry content digest differs')
            return json.loads(data), actual
    try:
        document, digest = manifest(role + '-' + source_id)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError('Registry lookup failed (HTTP ' + str(error.code) + ')') from None
    platform = document
    if 'manifests' in document:
        selected = [item for item in document['manifests'] if item.get('platform', {}).get('os') == 'linux'
                    and item.get('platform', {}).get('architecture') == 'amd64']
        if len(selected) != 1:
            raise ValueError('Role image platform is ambiguous')
        platform, _ = manifest(selected[0]['digest'])
    config_digest = platform['config']['digest']
    request = Request('https://ghcr.io/v2/' + repository + '/blobs/' + config_digest, headers=headers)
    with urlopen(request, timeout=20) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024 or 'sha256:' + hashlib.sha256(data).hexdigest() != config_digest:
        raise ValueError('Role image config differs')
    labels = json.loads(data)['config'].get('Labels', {})
    commit = labels.get('org.opencontainers.image.revision', '')
    if labels.get('traffic.role') != role or labels.get('traffic.source-id') != source_id or not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Role image labels differ from the checked source')
    return {'role': role, 'source_id': source_id, 'commit': commit, 'image': image + '@' + digest}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--role', required=True)
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    value = lookup(args.image, args.role, args.source_id)
    from pathlib import Path
    Path(args.output).write_text(json.dumps(value), encoding='utf-8')
    print(json.dumps({'role': args.role, 'reusable': value is not None}))
