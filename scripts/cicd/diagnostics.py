"""Bounded, allowlisted failure facts; never upload raw logs/spec/env."""
import argparse
import json
import os
from pathlib import Path
import re
import time

IDENTIFIER = re.compile(r'[A-Za-z0-9_.-]{1,253}')


def identifier(value):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else 'unknown'


def log_facts(raw):
    errors, frames = [], []
    for line in raw.splitlines():
        match = re.match(r'^([A-Za-z_][A-Za-z_0-9]{0,70}(?:Error|Exception))(?::|$)', line.strip())
        if match and match[1] not in errors:
            errors.append(match[1])
        match = re.search(r'File "[^"\r\n]*[/\\]([A-Za-z_][A-Za-z_0-9]{0,90}\.py)", line ([0-9]{1,6})', line)
        if match:
            frames.append({'file': match[1], 'line': int(match[2])})
    return {'error_types': errors[-8:], 'frames': frames[-10:]}


def collect(cluster, deployments, *, budget=12, clock=time.monotonic):
    deadline = clock() + budget
    result = {'schema': 'traffic-delivery-diagnostics/v1', 'pods': [], 'unavailable': [], 'raw_logs_included': False}
    def read(*args, json_output=True):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError('Diagnostic budget exhausted.')
        return cluster.call(*args, timeout=min(remaining, 4), json_output=json_output)
    try:
        pods = read('get', 'pods', '-o', 'json')['items']
    except Exception:
        result['unavailable'].append('pod_status')
        return result
    # Last changed deployment first: it is usually the failed rollout.
    names = list(reversed(deployments))
    for name in names:
        for pod in pods:
            metadata, status = pod.get('metadata', {}), pod.get('status', {})
            if not metadata.get('name', '').startswith(name + '-'):
                continue
            item = {'deployment': identifier(name), 'pod': identifier(metadata.get('name')),
                    'phase': identifier(status.get('phase')), 'containers': []}
            for entry in [*status.get('initContainerStatuses', []), *status.get('containerStatuses', [])]:
                state = entry.get('state', {})
                waiting, terminated = state.get('waiting', {}), state.get('terminated', {})
                previous = entry.get('lastState', {}).get('terminated', {})
                info = {'name': identifier(entry.get('name')), 'ready': entry.get('ready') is True,
                        'restarts': entry.get('restartCount', 0),
                        'waiting_reason': identifier(waiting.get('reason')) if waiting else None,
                        'termination_reason': identifier(terminated.get('reason')) if terminated else None,
                        'exit_code': terminated.get('exitCode'), 'previous_exit_code': previous.get('exitCode')}
                if waiting or terminated or not info['ready']:
                    for prior in (False, True):
                        if prior and not entry.get('restartCount'):
                            continue
                        try:
                            args = ['logs', metadata['name'], '-c', entry['name'], '--tail=120', '--limit-bytes=32768']
                            if prior:
                                args.append('--previous')
                            info['previous_log' if prior else 'log'] = log_facts(read(*args, json_output=False))
                        except Exception:
                            result['unavailable'].append(item['pod'] + '/' + info['name'] + ('/previous' if prior else '/current'))
                item['containers'].append(info)
            result['pods'].append(item)
    return result


def report(directory, commit, run_id, attempt, outcome):
    if not re.fullmatch('[0-9a-f]{40}', commit) or not run_id.isdigit() or not str(attempt).isdigit():
        raise ValueError('Invalid report identity.')
    root = Path(directory)
    journal = root / ('delivery-' + commit + '-' + run_id + '-' + str(attempt) + '.json')
    receipt = json.loads(journal.read_text(encoding='utf-8-sig')) if journal.is_file() else {}
    if receipt and receipt.get('commit') != commit:
        raise ValueError('Receipt belongs to another release.')
    success = outcome == 'success' and receipt.get('completed') is True
    if receipt.get('rolled_back') and not receipt.get('guard_held') and not receipt.get('recovery_required'):
        recovery = 'SUCCEEDED: previous specification restored'
    elif success:
        recovery = 'NOT NEEDED'
    elif not receipt.get('started') and not receipt.get('patched'):
        recovery = 'No recorded cluster mutation; inspect the failed step'
    else:
        recovery = 'OPERATOR CHECK REQUIRED'
    lines = ['## Backend deployment: ' + ('DEFERRED: waiting for idle training; not deployed' if receipt.get('deferred') else 'SUCCEEDED' if success else 'SKIPPED' if outcome == 'skipped' else 'FAILED'),
             '', '- Commit: `' + commit + '`', '- Recovery: **' + recovery + '**',
             '- Evidence retention: 30 days (sanitized receipt/diagnostics artifact).']
    if receipt.get('failed_phase') or receipt.get('phase'):
        lines.append('- Phase: `'+identifier(receipt.get('failed_phase') or receipt.get('phase'))+'`')
    execution=receipt.get('execution_failure',{})
    if execution:
        lines.append('- Runtime operation: `'+identifier(execution.get('container') or execution.get('workload'))+
                     '`; '+identifier(execution.get('error_type')))
    failure=receipt.get('probe_failure',{})
    if failure:
        line='- Read-only check: **'+identifier(failure.get('target'))+' / '+identifier(failure.get('endpoint'))+'**'
        status=failure.get('http_status')
        if type(status) is int and 100<=status<=599:line+=' (HTTP '+str(status)+')'
        lines.append(line+'; '+identifier(failure.get('error_type')))
    image = receipt.get('image', '')
    if isinstance(image, str) and re.fullmatch(r'ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}', image):
        lines.append('- Image: `' + image + '`')
    diagnostic = journal.with_suffix('.diagnostics.json')
    if diagnostic.is_file():
        data = json.loads(diagnostic.read_text(encoding='utf-8'))
        lines += ['', '| Pod | Container | State | Exit | Error types |', '|---|---|---|---|---|']
        for pod in data.get('pods', []):
            for container in pod.get('containers', []):
                if container.get('ready') and not container.get('waiting_reason') and container.get('exit_code') is None:
                    continue
                reason = container.get('waiting_reason') or container.get('termination_reason') or 'NotReady'
                errors = [*container.get('log', {}).get('error_types', []), *container.get('previous_log', {}).get('error_types', [])]
                exit_code = container.get('exit_code') if container.get('exit_code') is not None else container.get('previous_exit_code')
                lines.append('| ' + identifier(pod.get('pod')) + ' | ' + identifier(container.get('name')) +
                             ' | ' + identifier(reason) + ' | ' + str(exit_code) +
                             ' | ' + ', '.join(identifier(e) for e in errors) + ' |')
        if data.get('unavailable'):
            lines += ['', 'Some evidence was unavailable; collection never delays rollback beyond its short budget.']
    elif not success:
        lines += ['', 'Detailed pod evidence was unavailable. Use the failed step and the saved receipt; do not replay Apply blindly.']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='results/cicd')
    parser.add_argument('--outcome', required=True, choices=('success', 'failure', 'cancelled', 'skipped'))
    args = parser.parse_args()
    outcome = args.outcome
    if outcome == 'skipped' and not (os.environ.get('CLAIM_BEGAN') == 'false' and os.environ.get('CLAIM_OUTCOME') == 'success'):
        outcome = 'failure'
    text = report(args.directory, os.environ['EXPECTED_COMMIT'], os.environ['GITHUB_RUN_ID'],
                  os.environ['GITHUB_RUN_ATTEMPT'], outcome)
    print(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
            stream.write(text)


if __name__ == '__main__':
    main()
