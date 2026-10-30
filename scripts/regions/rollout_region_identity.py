"""Prepare the narrow MLflow region-identity fix; Apply is an operator action."""
import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.shap import rollout_neural_shap as base
from scripts.shap.check_neural_runtime import REMOTE as STARTUP
from scripts.shap.check_neural_compatibility import REMOTE as COMPATIBILITY
from scripts.shap.recover_feature_importance_cache import inventory
from scripts.cloud_training.prepare_rollout import canonical, code_map, sources
from scripts.cloud_training.manage_rollout import Rollout
from scripts.cloud_training.snapshot_cluster import capture

PRIOR = ROOT / 'results/feature-importance/five-model-v1'
DEFAULT = ROOT / 'results/region-maintenance/identity-v1'
SOURCE = 'backend/app/services/region_cleanup_resources.py'
ALLOWED = {'project/' + SOURCE}


def prepare(kubectl, directory):
    directory = Path(directory)
    if directory.exists():
        raise RuntimeError('Keep an existing review unchanged; use a fresh directory.')
    resources = base.read(PRIOR / 'configmaps.json')['items']
    previous = next(item for item in resources if item['metadata']['name'].startswith('traffic-cloud-')
                    and not item['metadata']['name'].startswith('traffic-cloud-airflow-'))
    old = base.code_manifest(previous)
    code, items, manifest = code_map(sources())
    changed = {name for name in set(old['files']) | set(manifest['files'])
               if old['files'].get(name) != manifest['files'].get(name)}
    if changed != ALLOWED:
        raise RuntimeError('Source changes differ from the single region-identity adapter.')
    cluster = Rollout(kubectl, base.PRIOR, base.PRIOR / 'baseline.json')
    cluster.verify(expected=True, bundle_id=old['bundle_id'])
    baseline = capture(kubectl)
    cache = next(v for v in baseline['deployments']['traffic-api']['volumes'] if v['name'] == 'artifact-cache')
    if cache.get('emptyDir', {}).get('sizeLimit') != '4Gi':
        raise RuntimeError('The reviewed 4Gi cache baseline differs.')
    capacity = inventory(type('CapacityCheck', (), {'cluster': cluster})())
    documents = {'baseline.json': baseline, 'capacity-at-prepare.json': capacity,
        'configmaps.json': {'apiVersion': 'v1', 'kind': 'List', 'items': [code,
            *[item for item in resources if item['metadata']['name'].startswith('traffic-cloud-airflow-')]]},
        'shap-dependency-config.json': base.read(PRIOR / 'shap-dependency-config.json')}
    specs = {}
    for name in base.ROLES:
        volume = next(item for item in baseline['deployments'][name]['volumes'] if item['name'] == 'cloud-code')
        if volume['configMap']['name'] != previous['metadata']['name']:
            raise RuntimeError('A running source bundle differs from the reviewed baseline.')
        replacement = copy.deepcopy(volume)
        replacement['configMap'].update(name=code['metadata']['name'], items=items)
        patch = {'spec': {'template': {'spec': {'volumes': [replacement]}}}}
        documents[name + '.patch.json'] = patch
        documents[name + '.rollback.json'] = {'spec': {'template': {'spec': {'volumes': [copy.deepcopy(volume)]}}}}
        before = cluster.call('get', 'deployment', name, '-o', 'json', json_result=True)
        after = cluster.call('patch', 'deployment', name, '--type=strategic', '--dry-run=server', '-o', 'json',
                             '--patch-file=-', content=json.dumps(patch), json_result=True)
        if before['metadata']['generation'] != baseline['deployments'][name]['generation']:
            raise RuntimeError('Deployment changed during prepare.')
        specs[name] = {'before': hashlib.sha256(canonical(before['spec'])).hexdigest(),
                       'after': hashlib.sha256(canonical(after['spec'])).hexdigest()}
    documents['deployment-spec-hashes.json'] = specs
    directory.mkdir(parents=True)
    review = {'bundle_id': manifest['bundle_id'], 'baseline_bundle_id': old['bundle_id'],
              'changed_files': sorted(changed), 'operator_sha256': base.digest(base.__file__), 'files': {},
              'preparer_sha256': base.digest(ROOT / 'scripts/cloud_training/prepare_rollout.py'),
              'identity_helper_sha256': base.digest(__file__)}
    for name, value in documents.items():
        (directory / name).write_text(json.dumps(value, indent=2), encoding='utf-8')
        review['files'][name] = base.digest(directory / name)
    (directory / 'review.json').write_text(json.dumps(review, indent=2), encoding='utf-8')
    return {'prepared': True, 'bundle_id': manifest['bundle_id'], 'cluster_changed': False}


class IdentityRollout(base.NeuralRollout):
    def __init__(self, kubectl, directory):
        super().__init__(kubectl, directory)
        if (set(self.review.get('changed_files', [])) != ALLOWED
                or self.review.get('identity_helper_sha256') != base.digest(__file__)):
            raise RuntimeError('Region-identity scope/helper changed after review.')
        self.journal_path = self.directory / 'identity-rollout-journal.json'
        self.journal = base.read(self.journal_path) if self.journal_path.exists() else {'bundle_id': self.review['bundle_id']}
        if self.journal['bundle_id'] != self.review['bundle_id']:
            raise RuntimeError('Wrong region-identity rollout state.')

    def probe(self, deployment):
        resources = base.read(self.directory / 'configmaps.json')['items']
        code = next(item for item in resources if item['metadata']['name'].startswith('traffic-cloud-')
                    and not item['metadata']['name'].startswith('traffic-cloud-airflow-'))
        patch = base.read(self.directory / (deployment + '.patch.json'))
        items = next(v['configMap']['items'] for v in patch['spec']['template']['spec']['volumes'] if v['name'] == 'cloud-code')
        role = 'api' if deployment == 'traffic-api' else 'tree' if deployment.endswith('tree') else 'recurrent'
        request = {'files': {item['path']: base64.b64encode(code['data'][item['key']].encode()).decode() for item in items},
                   'mode': 'api' if role == 'api' else 'worker', 'role': role, 'bundle_id': self.review['bundle_id']}
        if role == 'recurrent':
            request.update(dependency_config=base.read(self.directory / 'shap-dependency-config.json'),
                compat_program=COMPATIBILITY, compat_sources={name: base64.b64encode((ROOT / source).read_bytes()).decode()
                    for name, source in {'neural_shap_check': 'src/shap_reporting.py', 'recurrent_check': 'src/models/recurrent.py'}.items()})
        # Verify the new adapter actually overrides the old outer cleanup package.
        program = STARTUP.replace('  schema=app.openapi()', '''  import backend.app.services.region_cleanup_resources as cleanup
  assert Path(cleanup.__file__).resolve()==(root/"project/backend/app/services/region_cleanup_resources.py").resolve()
  assert cleanup.INSTALLED is True
  schema=app.openapi()''').replace('"source_startup_verified":True,', '"source_startup_verified":True,"cleanup_source_verified":True,')
        result = subprocess.run([self.cluster.kubectl, '--request-timeout=240s', '-n', 'traffic-mlops', 'exec', '-i',
            self.cluster.pod(deployment), '-c', self.baseline['runtime'][deployment]['main_container'], '--',
            'python', '-B', '-c', program], input=json.dumps(request), encoding='utf-8', capture_output=True, timeout=270)
        lines = [line.removeprefix('NEURAL_RUNTIME_PROOF ') for line in result.stdout.splitlines() if line.startswith('NEURAL_RUNTIME_PROOF ')]
        if result.returncode or len(lines) != 1:
            raise RuntimeError('Temporary startup check failed; no deployment changed.')
        proof = json.loads(lines[0])
        if not proof.get('cleanup_source_verified'):
            raise RuntimeError('The new cleanup adapter was not loaded.')
        proof['deployment'] = deployment
        (self.directory / (deployment + '.source-proof.json')).write_text(json.dumps(proof, indent=2), encoding='utf-8')
        return proof

    def check(self):
        result = super().validate()
        capacity = inventory(self)
        for name in base.ROLES:
            proof = base.read(self.directory / (name + '.source-proof.json'))
            if (proof.get('cleanup_source_verified') is not True or proof.get('source_startup_verified') is not True
                    or proof.get('bundle_id') != self.review['bundle_id'] or proof.get('fit_called') is not False
                    or proof.get('lifespan_not_started') is not True):
                raise RuntimeError('Three passing startup proofs are required for the exact new source.')
        return {**result, 'region_identity_ready_to_apply': True, 'startup_restore_bytes': capacity['restore_bytes']}

    def verify(self):
        result = super().verify()
        result.pop('production_ready', None)
        return {**result, 'region_identity_bundle_verified': True}

    def apply(self):
        result = super().apply()
        result.pop('neural_shap_applied', None)
        return {**result, 'region_identity_applied': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['Prepare', 'Probe', 'Validate', 'Check', 'Apply', 'Verify', 'Rollback'])
    parser.add_argument('--kubectl', default='C:/Users/ADMIN/AppData/Local/Programs/kubectl/kubectl.exe')
    parser.add_argument('--directory', default=str(DEFAULT))
    parser.add_argument('--deployment', choices=base.ROLES)
    args = parser.parse_args()
    try:
        if args.action == 'Prepare': result = prepare(args.kubectl, args.directory)
        else:
            operator = IdentityRollout(args.kubectl, args.directory)
            if args.action == 'Probe':
                if not args.deployment: raise RuntimeError('Choose a deployment for the temporary startup check.')
                result = operator.probe(args.deployment)
            else: result = getattr(operator, args.action.lower())()
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({'region_identity_stopped': True, 'error_type': type(error).__name__,
                          'safe_message': str(error) if isinstance(error, RuntimeError) else 'Raw details omitted.'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
