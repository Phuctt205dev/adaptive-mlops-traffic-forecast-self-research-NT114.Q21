"""Reviewed image inventory and isolated executor build contexts; no network or fits."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

ROOT = Path(__file__).resolve().parents[2]
FILE = 'deployment/cicd/components.json'
SCHEMA = 'traffic-components/v1'
LEGACY_ROLES = ('api', 'tree', 'neural', 'sync')
KINDS = {'api', 'worker', 'sidecar', 'runtime-job-image'}
BUILD_INPUT_ROOTS = ('infra/', 'src/', 'backend/', 'deployment/', 'scripts/', 'requirements/')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def safe_path(name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name or
            name.startswith('/') or any(p in {'', '.', '..'} for p in name.split('/'))):
        raise ValueError('Unsafe component input path')
    return name


def read(root, name):
    path = Path(root) / safe_path(name)
    if not path.resolve(strict=True).is_relative_to(Path(root).resolve()) or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Component inputs cannot follow symlinks')
    return path.read_bytes().replace(b'\r\n', b'\n')


def validate(value):
    if not isinstance(value, dict) or set(value) != {'schema', 'components'} or value['schema'] != SCHEMA:
        raise ValueError('Unsupported component inventory')
    entries = value['components']
    if not isinstance(entries, dict) or not set(LEGACY_ROLES) <= set(entries) or len(entries) > 32:
        raise ValueError('Core application components are required; retire explicitly')
    for name, spec in entries.items():
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', name) or not isinstance(spec, dict) or spec.get('kind') not in KINDS:
            raise ValueError('Invalid component identity/profile')
        executor = spec['kind'] == 'runtime-job-image'
        if executor == (name in LEGACY_ROLES):
            raise ValueError('Unsupported application profile; add runtime images using runtime-job-image')
        allowed = {'kind', 'entrypoints', 'resources', 'requirements'} | (
            {'dockerfile', 'check', 'consumers'} if executor else {'parent', 'ml_bundle', 'slots', 'replicas', 'service'})
        required = allowed - ({'slots', 'replicas', 'service'} if name == 'sync' else set())
        if set(spec) != required:
            raise ValueError('Unknown or missing component settings: ' + name)
        for key in ('entrypoints', 'resources', 'requirements'):
            if not isinstance(spec[key], list) or len(spec[key]) != len(set(spec[key])) or (key != 'resources' and not spec[key]):
                raise ValueError('Invalid component inputs')
        for module in spec['entrypoints']:
            if not re.fullmatch(r'(?:src|backend)(?:\.[A-Za-z_][A-Za-z0-9_]*)+', module):
                raise ValueError('Invalid component entrypoint')
        for path in spec['resources'] + spec['requirements']:
            safe_path(path)
        if executor:
            safe_path(spec['dockerfile']); safe_path(spec['check'])
            # These roots are included in the protected-source freshness check.
            # Reject unsupported layouts instead of deploying stale inputs that
            # happened to live outside the known build tree.
            if (not spec['dockerfile'].startswith(BUILD_INPUT_ROOTS) or
                    not spec['check'].startswith(BUILD_INPUT_ROOTS) or
                    any(not path.startswith('requirements/') for path in spec['requirements']) or
                    any(not path.startswith(BUILD_INPUT_ROOTS) for path in spec['resources'])):
                raise ValueError('Execution inputs must use tracked build input roots')
            if not isinstance(spec['consumers'], list) or not spec['consumers'] or len(spec['consumers']) != len(set(spec['consumers'])) or not set(spec['consumers']) <= {'api', 'tree', 'neural'}:
                raise ValueError('Unknown runtime image consumer')
        else:
            if spec['parent'] not in {'db', 'web', 'cloud'} or type(spec['ml_bundle']) is not bool:
                raise ValueError('Invalid application build profile')
            if spec['kind'] != {'api': 'api', 'tree': 'worker', 'neural': 'worker', 'sync': 'sidecar'}[name]:
                raise ValueError('Application profile changed')
            if name != 'sync' and (not isinstance(spec['slots'], list) or len(spec['slots']) != 2 or
                    any(not re.fullmatch('[a-z][a-z0-9-]*', s) for s in spec['slots']) or
                    len(set(spec['slots'])) != 2 or type(spec['replicas']) is not int or spec['replicas'] not in {1, 2} or
                    not re.fullmatch('[a-z][a-z0-9-]*', spec['service'])):
                raise ValueError('Invalid application runtime topology')
    return value


def load(root=ROOT):
    return validate(json.loads(read(root, FILE)))


def applications(value=None):
    entries = (value or load())['components']
    # Stable build-stage order; JSON object formatting/order is not an input.
    return tuple(name for name in LEGACY_ROLES if entries[name]['kind'] != 'runtime-job-image')


def executors(value=None):
    return {name: spec for name, spec in (value or load())['components'].items() if spec['kind'] == 'runtime-job-image'}


def requirements(root, names):
    """Hash all nested -r/-c inputs, resolving includes relative to their file."""
    found, active = {}, set()
    def visit(name):
        safe_path(name)
        if name in active:
            raise ValueError('Cyclic requirement includes')
        if name in found:
            return
        active.add(name)
        data = read(root, name)
        for line in data.decode().splitlines():
            line = line.split('#', 1)[0].strip()
            match = re.fullmatch(r'(?:-r|-c|--requirement|--constraint)\s+([^\s]+)', line)
            if match:
                child = str(PurePosixPath(name).parent / match[1])
                visit(child)
            elif line.startswith(('-r', '-c', '--requirement', '--constraint')):
                raise ValueError('Requirement includes must use a local relative file')
        found[name] = data
        active.remove(name)
    for name in names:
        visit(name)
    return found


def prepare_executor(root, name, spec, closure, commit):
    files = closure(root, name, entrypoints=spec['entrypoints'], resources=spec['resources'], allow_training=True)
    files.update(requirements(root, spec['requirements']))
    recipe = read(root, spec['dockerfile'])
    if not re.search(rb'^FROM docker\.io/library/python:[\w.-]+@sha256:[0-9a-f]{64}\s*$', recipe, re.M):
        raise ValueError('Execution image base must be pinned')
    files['executor_check.py'] = read(root, spec['check'])
    files['Dockerfile'] = recipe + b'\nCOPY executor-release.json executor_check.py /app/\n'
    metadata = {'schema': 'traffic-executor-image/v1', 'role': name, 'profile': spec,
                'files': {p: digest(data) for p, data in sorted(files.items())}}
    metadata['source_id'] = digest(canonical(metadata))
    metadata['commit'] = commit
    files['executor-release.json'] = canonical(metadata)
    return files, metadata


def validate_executors(release, binding):
    wanted = release.get('executors', {})
    actual = binding.get('executors', {})
    if not isinstance(actual, dict) or set(actual) != set(wanted):
        raise ValueError('Missing or unexpected execution image binding')
    for name, spec in wanted.items():
        value = actual[name]
        if (value.get('role') != name or value.get('source_id') != spec['source_id'] or
                not re.fullmatch(r'ghcr.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}', value.get('image', '')) or
                not re.fullmatch('[0-9a-f]{40}', value.get('commit', ''))):
            raise ValueError('Execution image identity differs')
    if wanted and binding.get('component_registry_sha256') != release['component_registry_sha256']:
        raise ValueError('Execution inventory binding differs')
    return actual


def local_config(value):
    keys = {'schema', 'component', 'enabled', 'namespace', 'memory_mib', 'cpu', 'reserve_mib',
            'deadline_seconds', 'service_account', 'dispatcher_account'}
    if (not isinstance(value, dict) or set(value) != keys or value['schema'] != 'traffic-local-training-config/v1' or
            type(value['enabled']) is not bool or value['namespace'] != 'traffic-mlops' or
            value['service_account'] != 'traffic-local-training' or value['dispatcher_account'] != 'traffic-training-dispatcher'):
        raise ValueError('Invalid Local runtime configuration')
    for key, low, high in [('memory_mib', 1024, 6144), ('cpu', 1, 2), ('reserve_mib', 1024, 16384), ('deadline_seconds', 60, 28800)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError('Invalid Local resource budget')
    if not re.fullmatch('[a-z][a-z0-9-]{0,39}', value['component']):
        raise ValueError('Invalid Local execution component')
    return value


def main():
    """Emit matrix data only; never accept commands from registry values."""
    import argparse
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='release')
    args = parser.parse_args()
    release = json.loads((Path(args.directory) / 'release.json').read_bytes())
    value = validate(json.loads((Path(args.directory) / 'components.json').read_bytes()))
    outputs = {'applications': list(applications(value)), 'executors': list(executors(value))}
    if set(release['roles']) != set(outputs['applications']) or set(release['executors']) != set(outputs['executors']):
        raise ValueError('Release inventory differs')
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
        for key, values in outputs.items():
            stream.write(key + '=' + json.dumps(values, separators=(',', ':')) + '\n')


if __name__ == '__main__':
    main()
