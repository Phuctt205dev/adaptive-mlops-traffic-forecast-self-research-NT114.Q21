"""Operator-run READ-ONLY cluster proof, then release a stranded CD ownership.

Repair/Rollback must already have completed using its original artifact/journal.
This command never repairs pods, edits receipts, unpauses a DAG or trains.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.cicd.cd_state import GitHub, Store, emit
from scripts.cicd.deploy import Cluster, spec_hash, ready_pods
from scripts.cloud_training.manage_rollout import READINESS

NAMES = {'traffic-api', 'traffic-training-tree', 'traffic-training-worker', 'airflow-scheduler', 'airflow-webserver'}


def confirm(cluster, receipt, active):
    if receipt.get('commit') != active['commit']:
        raise ValueError('The original receipt belongs to another transaction.')
    plans = receipt.get('plans', {})
    if set(plans) != NAMES:
        raise ValueError('Recovery requires the original five deployment plans.')
    namespace = cluster.call('get', 'namespace', 'traffic-mlops', '-o', 'json')
    if namespace['metadata']['uid'] != cluster.namespace_uid:
        raise ValueError('Wrong namespace identity.')
    pods = cluster.call('get', 'pods', '-o', 'json')['items']
    # An uncertain transaction may be reconciled only to its exact recorded
    # BEFORE specs. Do not edit the original receipt to pretend it is closed.
    field = 'after' if receipt.get('completed') and not receipt.get('rolled_back') else 'before'
    for name in sorted(NAMES):
        deployment = cluster.deployment(name)
        plan = plans[name]
        if deployment['metadata']['uid'] != plan['uid'] or spec_hash(deployment) != plan[field]:
            raise ValueError('Deployment no longer matches the recovery receipt: ' + name)
        if len(ready_pods(deployment, pods)) != deployment['spec'].get('replicas', 1):
            raise ValueError('A deployment is not fully Ready: ' + name)
    if receipt.get('guard_job'):
        job = cluster.call('get', 'job', receipt['guard_job'], '--ignore-not-found=true', '-o', 'json')
        if job and not any(c.get('type') in {'Complete', 'Failed'} and c.get('status') == 'True'
                           for c in job.get('status', {}).get('conditions', [])):
            raise ValueError('Original guard job is still active or unconfirmed.')
    if receipt.get('guard_pod') or receipt.get('guard_job'):
        guards = [p for p in pods if p['metadata']['name'] == receipt.get('guard_pod')
                  or receipt.get('guard_job') and p['metadata'].get('labels', {}).get('job-name') == receipt['guard_job']]
        if guards and guards[0].get('status', {}).get('phase') not in {'Succeeded', 'Failed'}:
            raise ValueError('Original guard has not terminated.')
    readiness = cluster.remote(cluster.deployment('traffic-api'), READINESS)
    if readiness['dag_paused'] != receipt.get('dag_was_paused') or readiness['active_controlled_runs'] or readiness['active_airflow_runs']:
        raise ValueError('DAG/training state is not the recorded recovered state.')
    return {'recovery_verified': True, 'deployments_ready': 5}


def release_ownership(store, active, proof):
    if proof.get('recovery_verified') is not True or proof.get('deployments_ready') != 5:
        raise ValueError('Read-only recovery proof is required.')
    def change(state):
        if state['active'] != active:
            raise ValueError('Recovery ownership changed; inspect again.')
        state['active'] = None
        state['blocked'] = True
        if state['failure']:
            state['failure']['recovery_required'] = False
        state['updated_by'] = 'operator-recovery'
        return state, {'recovery_verified': True, 'auto_blocked': True,
                       'next_step': 'Use Manual for a verified release before enabling Auto.'}
    return store.update(change, message='Record verified operator recovery ' + active['run_id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', required=True)
    parser.add_argument('--kubectl', required=True)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--namespace-uid', required=True)
    args = parser.parse_args()
    store = Store(GitHub(os.environ['GITHUB_REPOSITORY']))
    active = store.load()[0]['active']
    if active is None:
        raise ValueError('No stranded ownership exists; no recovery change is needed.')
    receipt = json.loads(Path(args.receipt).read_text(encoding='utf-8-sig'))
    cluster = Cluster(args.kubectl, args.context, args.namespace_uid, args.kubeconfig)
    emit(release_ownership(store, active, confirm(cluster, receipt, active)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'recovery_confirm_failed': True, 'error_type': type(error).__name__,
                          'reason': str(error) if isinstance(error, ValueError) else 'Details omitted.'}))
        raise SystemExit(1)
