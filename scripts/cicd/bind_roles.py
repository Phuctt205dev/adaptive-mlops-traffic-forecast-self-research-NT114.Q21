"""Bind successfully checked role image digests to the exact CI release."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.cicd.roles_release import verify, ROLES


def bind(directory, bindings, commit):
    root = Path(directory)
    release = verify(root, expected_commit=commit)
    found = {}
    for path in Path(bindings).rglob('image-*.json'):
        value = json.loads(path.read_text(encoding='utf-8'))
        role = value.get('role')
        if (role not in ROLES or role in found or value.get('source_id') != release['roles'][role]['source_id']
                or not re.fullmatch(r'ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}', value.get('image',''))
                or not re.fullmatch('[0-9a-f]{40}', value.get('commit',''))):
            raise ValueError('Duplicate or invalid role image binding')
        found[role] = value
    if set(found) != set(ROLES):
        raise ValueError('A checked role image is missing')
    result = {'schema': 'traffic-delivery/v2', 'commit': commit, 'release_id': release['release_id'],
              'image': found['api']['image'], 'images': found}
    (root / 'delivery.json').write_text(json.dumps(result), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--bindings', required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    result = bind(args.directory, args.bindings, args.commit)
    print(json.dumps({'commit': result['commit'], 'roles': sorted(result['images'])}))
