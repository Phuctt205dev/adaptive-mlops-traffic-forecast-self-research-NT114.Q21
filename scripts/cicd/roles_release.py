"""Build role-scoped source contexts. No credentials, cluster or training calls."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import re

from src.cloud_training.package import ml_sources

SCHEMA = 'traffic-release/v2'
ROLES = ('api', 'tree', 'neural', 'sync')
ROOTS = {
    'api': ['backend.app.services.serving_runtime', 'backend.app.services.lifecycle',
            'backend.app.services.region_cleanup_runtime', 'backend.app.api.routes.training_monitor'],
    'tree': ['backend.app.services.worker_runtime', 'backend.app.services.region_cleanup_runtime',
             'src.cloud_training.kaggle_rpc'],
    'neural': ['backend.app.services.worker_runtime', 'backend.app.services.region_cleanup_runtime',
               'src.cloud_training.kaggle_rpc'],
    'sync': ['backend.app.services.region_cleanup_uploader', 'backend.app.services.restore_cache'],
}
FORBIDDEN = {'src/pipeline.py', 'src/time_series_training.py', 'src/lstm_sequences.py',
             'backend/app/api/routes/internal_training.py'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def closure(root, role):
    """Include local imports, with explicit dynamic entrypoints per role."""
    root = Path(root)
    result, visited, pending = {}, set(), list(ROOTS[role])

    def file_for(module):
        base = root.joinpath(*module.split('.'))
        if base.with_suffix('.py').is_file():
            return base.with_suffix('.py')
        if (base / '__init__.py').is_file():
            return base / '__init__.py'
        return None

    def enqueue(module):
        if module in {'app', 'src', 'backend'} or module.startswith(('src.', 'backend.')):
            if file_for(module):
                pending.append(module)
            parts = module.split('.')
            for length in range(1, len(parts)):
                parent = '.'.join(parts[:length])
                if file_for(parent):
                    pending.append(parent)

    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        path = file_for(module)
        if path is None:
            raise ValueError('A role entrypoint is missing: ' + module)
        if path.is_symlink():
            raise ValueError('Role sources cannot follow symlinks')
        name = path.relative_to(root).as_posix()
        if name in FORBIDDEN:
            raise ValueError('Role imports training-only implementation: ' + name)
        if role == 'api' and name in {'backend/app/services/training_control.py',
                                     'backend/app/services/worker_runtime.py'}:
            raise ValueError('API imports a worker executor')
        data = path.read_bytes().replace(b'\r\n', b'\n')
        result[name] = data
        tree = ast.parse(data, filename=name)
        nodes = tree.body if role == 'sync' else ast.walk(tree)
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    enqueue(alias.name)
            elif isinstance(node, ast.ImportFrom):
                target = node.module or ''
                if node.level:
                    package = module if path.name == '__init__.py' else module.rsplit('.', 1)[0]
                    target = importlib.util.resolve_name('.' * node.level + target, package)
                enqueue(target)
                for alias in node.names:
                    enqueue(target + '.' + alias.name)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module':
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    enqueue(node.args[0].value)
    return result


def dockerfile(base):
    header = f'''FROM {base} AS db
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libstdc++6 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements/constraints.txt requirements/db.txt /tmp/role-requirements/
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install -r /tmp/role-requirements/db.txt
FROM db AS web
COPY requirements/web.txt /tmp/role-requirements/web.txt
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install -r /tmp/role-requirements/web.txt
FROM web AS cloud
COPY requirements/sdk.txt /tmp/role-requirements/sdk.txt
RUN python -m venv /opt/kaggle-sdk
RUN --mount=type=cache,target=/root/.cache/pip /opt/kaggle-sdk/bin/python -m pip install -r /tmp/role-requirements/sdk.txt
ENV KAGGLE_PYTHON=/opt/kaggle-sdk/bin/python
'''
    output = header
    for role in ROLES:
        parent = 'db' if role == 'sync' else 'web' if role == 'api' else 'cloud'
        output += f'FROM {parent} AS {role}\n'
        if role != 'sync':
            output += f'COPY requirements/{role}.txt /tmp/role-requirements/{role}.txt\n'
            output += f'RUN --mount=type=cache,target=/root/.cache/pip python -m pip install -r /tmp/role-requirements/{role}.txt\n'
        output += f'ENV TRAFFIC_IMAGE_ROLE={role}\nCOPY roles/{role}/ /app/\n'
        if role == 'sync':
            output += 'RUN python -B -c "from deployment.rke2.runtime.role_runtime import verify; verify(); import backend.app.services.region_cleanup_uploader"\n'
        else:
            output += 'RUN python -B -c "from deployment.rke2.runtime.role_runtime import verify; verify()"\n'
        output += 'CMD ["python","-B","/app/deployment/rke2/runtime/role_runtime.py"]\n'
    return output.encode()


def build(root, output, commit):
    root, output = Path(root), Path(output)
    if output.exists() or not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Use a fresh output and explicit commit')
    baseline = json.loads((root / 'deployment/rke2/role-baseline.json').read_text(encoding='utf-8'))
    if not re.fullmatch(r'docker\.io/library/python:[a-z0-9.-]+@sha256:[0-9a-f]{64}', baseline['python_base']):
        raise ValueError('Python role base must be pinned')
    ml = ml_sources(root)
    ml['scripts/cloud_training/verify_execution.py'] = (root / 'scripts/cloud_training/verify_execution.py').read_bytes().replace(b'\r\n', b'\n')
    bundle_id = digest(canonical({name: digest(data) for name, data in sorted(ml.items())}))
    payload = {}
    scopes = {}
    requirements = {path.name: path.read_bytes().replace(b'\r\n', b'\n')
                    for path in (root / 'requirements/roles').glob('*.txt')}
    bootstrap = 'deployment/rke2/runtime/role_runtime.py'
    for role in ROLES:
        files = closure(root, role)
        files[bootstrap] = (root / bootstrap).read_bytes().replace(b'\r\n', b'\n')
        if role == 'sync':
            files['scripts/cicd/roles_guard.py'] = (root / 'scripts/cicd/roles_guard.py').read_bytes().replace(b'\r\n', b'\n')
        if role == 'api':
            files['docs/index.html'] = (root / 'docs/index.html').read_bytes()
        if role in {'tree', 'neural'}:
            files.update({'kaggle-project/' + name: data for name, data in ml.items()})
        source_hashes = {name: digest(data) for name, data in sorted(files.items())}
        used_requirements = ['constraints.txt','db.txt'] + ([] if role == 'sync' else ['web.txt',role+'.txt'])
        if role in {'tree','neural'}:
            used_requirements.append('sdk.txt')
        metadata = {'schema': 'traffic-role-runtime/v1', 'role': role,
                    'bundle_id': bundle_id if role in {'tree','neural'} else digest(canonical(source_hashes)),
                    'base_image': baseline['python_base'],
                    'producer_profile': baseline['producer_profile'] if role in {'tree','neural'} else {},
                    'dependency_hashes': {name:digest(requirements[name]) for name in used_requirements},
                    'recipe_sha256': digest(dockerfile(baseline['python_base'])),
                    'files': source_hashes}
        metadata['source_id'] = digest(canonical(metadata))
        metadata['commit'] = commit
        files['role-release.json'] = canonical(metadata)
        scopes[role] = {'deployment': baseline['roles'][role].get('deployment'),
                        'metadata_sha256': digest(files['role-release.json']), 'source_count': len(files),
                        'source_id': metadata['source_id'], 'bundle_id': metadata['bundle_id']}
        payload.update({'roles/' + role + '/' + name: data for name, data in files.items()})
    for path in (root / 'requirements/roles').glob('*.txt'):
        payload['requirements/' + path.name] = path.read_bytes().replace(b'\r\n', b'\n')
    payload['Dockerfile'] = dockerfile(baseline['python_base'])
    payload['.dockerignore'] = b'**\n!Dockerfile\n!release.json\n!roles/\n!roles/**\n!requirements/\n!requirements/**\n'
    # Keep the Airflow protocol source independent from application role images.
    for name, source in [('dag.py', 'deployment/rke2/runtime/traffic_region_training_cloud.py'),
                         ('legacy/traffic_region_training_worker.py', 'deployment/rke2/runtime/traffic_region_training_worker.py'),
                         ('airflow_control.py', 'src/cloud_training/airflow_control.py')]:
        payload['airflow/' + name] = (root / source).read_bytes().replace(b'\r\n', b'\n')
    document = {'schema': SCHEMA, 'commit': commit, 'baseline': baseline, 'cloud_bundle_id': bundle_id,
                'roles': scopes, 'files': {name: digest(data) for name, data in sorted(payload.items())}}
    document['release_id'] = digest(canonical(document))
    for name, data in payload.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (output / 'release.json').write_bytes(canonical(document))
    verify(output, expected_commit=commit)
    return document


def verify(directory, *, expected_commit=None):
    root = Path(directory).resolve(strict=True)
    document = json.loads((root / 'release.json').read_bytes())
    if document.get('schema') != SCHEMA or set(document.get('roles', {})) != set(ROLES):
        raise ValueError('Unsupported role release')
    if not re.fullmatch('[0-9a-f]{40}', document.get('commit', '')) or (expected_commit and document['commit'] != expected_commit):
        raise ValueError('Role release commit differs')
    identity = {k: v for k, v in document.items() if k != 'release_id'}
    if document.get('release_id') != digest(canonical(identity)):
        raise ValueError('Role release metadata was modified')
    if any(p.is_symlink() for p in root.rglob('*')):
        raise ValueError('Role release cannot contain symlinks')
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if actual != set(document['files']) | {'release.json'}:
        raise ValueError('Role release files differ')
    for name, wanted in document['files'].items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
            raise ValueError('Unsafe role release path')
        if digest((root / path).read_bytes()) != wanted:
            raise ValueError('Role release source differs')
    for role in ROLES:
        metadata = json.loads((root / 'roles' / role / 'role-release.json').read_bytes())
        content = {k:v for k,v in metadata.items() if k not in {'source_id','commit'}}
        if (metadata['role'] != role or metadata['commit'] != document['commit']
                or metadata['source_id'] != digest(canonical(content))
                or metadata['source_id'] != document['roles'][role]['source_id']
                or metadata['bundle_id'] != document['roles'][role]['bundle_id']):
            raise ValueError('Role binding differs')
    return document
