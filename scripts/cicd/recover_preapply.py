"""Prove a historical package-verification failure before releasing CD ownership.

Default is read-only. --release records verified recovery through the existing
CAS controller; Auto remains blocked. No Kubernetes writes or synthetic receipt.
Only the reviewed pre-download-helper controller is supported by this adapter.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.cicd.cd_state import GitHub, Store, emit
from scripts.cicd.deploy import Cluster
from scripts.cicd.prepare_artifact import GitHubArtifactClient, archive_files, download
from scripts.cicd.recover_cd import confirm_roles, release_ownership

# Reviewed historical code: Verify/throw at line 28 precedes any engine call.
# Unknown controller versions must use their own reviewed recovery procedure.
CONTROLLERS = {
    'scripts/cicd/deliver.ps1': '59dd0f40350c024cf4c5cd43da4e3fa3d2ed540c21a2cc32495ea5d032fe4ac6',
    '.github/workflows/backend-deploy.yml': '1036232a018a36fc62951875979b8261eaa1cf202d76eda438d834a2dd948bb5',
}
DEPLOYMENTS = {'api', 'api-next', 'train-tree', 'tree-next', 'train-neural', 'neural-next',
               'airflow-scheduler', 'airflow-webserver'}
SERVICES = {'traffic-api', 'traffic-api-training', 'api-peers', 'api-preview',
            'traffic-training-tree', 'traffic-training-worker'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def run_identity(client, transaction, conclusion):
    run = client.get('actions/runs/' + transaction['run_id'])
    if (str(run.get('id')) != transaction['run_id'] or run.get('run_attempt') != transaction['attempt']
            or run.get('head_sha') != transaction['commit'] or run.get('status') != 'completed'
            or run.get('conclusion') != conclusion or run.get('event') not in {'workflow_run', 'workflow_dispatch'}
            or run.get('path') != '.github/workflows/backend-deploy.yml'
            or (run.get('head_repository') or {}).get('full_name', '').lower() != client.repository.lower()):
        raise ValueError('CD run identity, attempt or terminal outcome differs.')
    return run


def artifacts(client, run_id):
    value = client.get(f'actions/runs/{run_id}/artifacts?per_page=100')
    entries = value.get('artifacts', [])
    if value.get('total_count') != len(entries) or len(entries) >= 100:
        raise ValueError('Incomplete artifact listing; manual review required.')
    return entries


def prove_failure(client, active):
    run_identity(client, active, 'failure')
    for path, expected in CONTROLLERS.items():
        source = client.get(f'contents/{path}?ref={active["commit"]}')
        if source.get('type') != 'file' or source.get('encoding') != 'base64':
            raise ValueError('Original delivery controller is unavailable.')
        raw = base64.b64decode(''.join(source['content'].split()), validate=True)
        if sha(raw.replace(b'\r\n', b'\n')) != expected:
            raise ValueError('Unknown original controller; pre-Apply recovery is unsupported.')
    data = client.get(f'actions/runs/{active["run_id"]}/attempts/{active["attempt"]}/jobs?per_page=100')
    jobs = data.get('jobs', [])
    if (data.get('total_count') != 3 or len(jobs) != 3
            or {j.get('name') for j in jobs} != {'inspect', 'deploy', 'record-result'}
            or any(j.get('status') != 'completed' for j in jobs)):
        raise ValueError('CD jobs are incomplete or still active.')
    by_name = {j['name']: j for j in jobs}
    job = by_name['deploy']
    if by_name['inspect'].get('conclusion') != 'success' or job.get('conclusion') != 'failure':
        raise ValueError('CD did not fail at the expected delivery boundary.')
    steps = job.get('steps', [])
    matches = lambda name, outcome: [s for s in steps if s.get('name') == name and s.get('conclusion') == outcome]
    if (len(matches('Claim this deployment under the saved mode', 'success')) != 1
            or len(matches('Deliver the approved immutable release', 'failure')) != 1
            or any(s.get('status') != 'completed' for s in steps)):
        raise ValueError('The expected claim/delivery steps were not confirmed.')
    with client.open(f'actions/jobs/{job["id"]}/logs') as response:
        parts, size = [], 0
        reader = getattr(response, 'read1', response.read)
        while True:
            client.deadline.timeout()
            chunk = reader(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > 8 * 1024 * 1024:
                raise ValueError('Delivery log exceeds the evidence budget.')
            parts.append(chunk)
    raw = b''.join(parts)
    log = re.sub(r'\x1b\[[0-9;]*m', '', raw.decode('utf-8-sig'))
    if (not re.search(r'deliver\.ps1:28 char:28', log)
            or not re.search(r'(?m)^\d{4}-\d\d-\d\dT\S+ ' + re.escape(active['commit']) + r'\s*$', log)
            or 'ValueError: Role release files differ' not in log
            or 'Release file verification failed.' not in log
            or '"began": true' not in log
            or '"delivery_phase"' in log or 'roles_deploy.py' in log):
        raise ValueError('Logs do not prove the reviewed verification failure before engine entry.')
    # A real journal must use normal receipt recovery, even if the log looks safe.
    if any(a.get('name', '').startswith('delivery-receipt-') for a in artifacts(client, active['run_id'])):
        raise ValueError('A receipt artifact exists; use original receipt recovery instead.')
    return {'failed_run': active['run_id'], 'failed_attempt': active['attempt'], 'log_sha256': sha(raw),
            'controllers': CONTROLLERS, 'engine_not_entered': True}


def baseline_receipt(client, previous):
    run_identity(client, previous, 'success')
    name = f'delivery-receipt-{previous["commit"]}-{previous["attempt"]}'
    found = [a for a in artifacts(client, previous['run_id']) if a.get('name') == name]
    if len(found) != 1 or found[0].get('expired') is not False:
        raise ValueError('The last successful original receipt is missing or ambiguous.')
    artifact = found[0]
    identity = artifact.get('workflow_run') or {}
    if (str(identity.get('id')) != previous['run_id'] or identity.get('head_sha') != previous['commit']
            or type(artifact.get('id')) is not int or artifact['id'] < 1
            or type(artifact.get('size_in_bytes')) is not int or not 0 < artifact['size_in_bytes'] <= 64 * 1024 * 1024
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', artifact.get('digest', ''))):
        raise ValueError('Baseline artifact identity or digest differs.')
    with tempfile.TemporaryDirectory(prefix='traffic-recovery-') as directory:
        archive_path = download(client, artifact, Path(directory))
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive_files(archive)
            expected = f'delivery-{previous["commit"]}-{previous["run_id"]}-{previous["attempt"]}.json'
            selected = [i for i in entries if i.filename == expected]
            if len(selected) != 1 or selected[0].file_size > 2 * 1024 * 1024:
                raise ValueError('Original baseline journal is missing or exceeds its budget.')
            raw = archive.read(selected[0])  # CRC is checked while reading.
    receipt = json.loads(raw.decode('utf-8-sig'))
    wanted = {('deployment', n) for n in DEPLOYMENTS} | {('service', n) for n in SERVICES}
    resources = receipt.get('resources', [])
    if (receipt.get('schema') != 'traffic-role-delivery/v2' or receipt.get('commit') != previous['commit']
            or receipt.get('completed') is not True or receipt.get('rolled_back') is not False
            or receipt.get('guard_held') is not False or receipt.get('recovery_required') is not False
            or len(resources) != len(wanted) or {(p.get('kind'), p.get('name')) for p in resources} != wanted):
        raise ValueError('Baseline must be the complete successful role receipt with all fourteen resources.')
    return receipt, {'baseline_run': previous['run_id'], 'baseline_commit': previous['commit'],
                     'baseline_artifact': artifact['id'], 'baseline_zip_digest': artifact['digest'],
                     'baseline_receipt_sha256': sha(raw)}


def prove(client, cluster, state):
    active, previous = state['active'], state['last_success']
    if not active or not previous or not active.get('recovery_required') or not state['blocked']:
        raise ValueError('A blocked stranded owner and last successful baseline are required.')
    if previous['ci_number'] >= active['ci_number']:
        raise ValueError('The baseline must precede the stranded release.')
    failed = prove_failure(client, active)
    receipt, baseline = baseline_receipt(client, previous)
    proof = confirm_roles(cluster, receipt)
    jobs = cluster.call('get', 'jobs', '-o', 'json')['items']
    for job in jobs:
        if job['metadata']['name'].startswith('delivery-') and not any(
                c.get('type') in {'Complete', 'Failed'} and c.get('status') == 'True'
                for c in job.get('status', {}).get('conditions', [])):
            raise ValueError('A delivery admission job is still active or unconfirmed.')
    pods = cluster.call('get', 'pods', '-o', 'json')['items']
    for pod in pods:
        metadata = pod['metadata']
        if (metadata['name'].startswith('delivery-')
                or metadata.get('labels', {}).get('job-name', '').startswith('delivery-')):
            if pod.get('status', {}).get('phase') not in {'Succeeded', 'Failed'}:
                raise ValueError('A delivery admission pod is still active or unconfirmed.')
    run_identity(client, active, 'failure')  # Refuse a concurrent rerun.
    run_identity(client, previous, 'success')
    return {'schema': 'traffic-preapply-recovery-proof/v1', 'namespace_uid': cluster.namespace_uid,
            'active': active, **failed, **baseline, **proof}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('kubectl', 'kubeconfig', 'context', 'namespace-uid', 'proof-output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--release', action='store_true', help='Record recovery after fresh proof; Auto stays blocked.')
    args = parser.parse_args()
    repository = os.environ['GITHUB_REPOSITORY']
    store = Store(GitHub(repository))
    state = store.load()[0]
    client = GitHubArtifactClient(repository, os.environ['GH_TOKEN'])
    cluster = Cluster(args.kubectl, args.context, args.namespace_uid, args.kubeconfig)
    proof = prove(client, cluster, state)
    output = Path(args.proof_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
    if args.release:
        emit(release_ownership(store, state['active'], proof, expected_success=state['last_success']))
    else:
        print(json.dumps({'recovery_proven': True, 'state_changed': False, 'proof': str(output)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'recovery_confirm_failed': True, 'error_type': type(error).__name__,
                          'reason': str(error) if isinstance(error, ValueError) else 'Details omitted.'}))
        raise SystemExit(1)
