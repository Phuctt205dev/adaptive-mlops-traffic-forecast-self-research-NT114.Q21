"""Persistent CD policy and transaction ownership on a configuration-only branch.

Only this JSON is read from cd-settings; no code from that branch is executed.
Writes use the Contents API SHA as a compare-and-swap, never force-push refs.
"""
import argparse
import base64
import copy
import json
import os
from pathlib import Path
import re
import ssl
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BRANCH = 'cd-settings'
FILE = 'cd-state.json'
ENVIRONMENTS = {'manual': 'rke2-production', 'auto': 'rke2-production-auto'}
SHA = re.compile(r'[0-9a-f]{40}')


class ApiError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__('GitHub request failed (HTTP ' + str(status) + '); response omitted.')


class GitHub:
    def __init__(self, repository, token=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Invalid repository.')
        self.repository = repository
        self.token = token or os.environ['GH_TOKEN']

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request('https://api.github.com/repos/' + self.repository + ('/' + path if path else ''),
                          data=data, method=method, headers={
                              'Authorization': 'Bearer ' + self.token,
                              'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                              'Cache-Control': 'no-cache',
                              'X-GitHub-Api-Version': '2022-11-28'})
        try:
            context = ssl.create_default_context()
            # The runner already exports the public roots trusted by Windows
            # for Node actions. Reuse those roots without disabling TLS checks.
            if os.name == 'nt' and os.environ.get('NODE_EXTRA_CA_CERTS'):
                context.load_verify_locations(cafile=os.environ['NODE_EXTRA_CA_CERTS'])
            with urlopen(request, timeout=30, context=context) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError('GitHub response exceeds its budget.')
                return json.loads(raw) if raw else {}
        except HTTPError as error:
            raise ApiError(error.code) from None


def initial():
    return {'schema': 'traffic-cd-control/v1', 'mode': 'manual', 'epoch': 0,
            'blocked': False, 'active': None, 'failure': None, 'last_success': None,
            'last_control': 0, 'updated_by': 'bootstrap'}


def validate(value):
    if not isinstance(value, dict) or set(value) != set(initial()):
        raise ValueError('Unsupported CD state fields.')
    if (value['schema'] != initial()['schema'] or value['mode'] not in ENVIRONMENTS
            or type(value['epoch']) is not int or value['epoch'] < 0
            or type(value['last_control']) is not int or value['last_control'] < 0
            or type(value['blocked']) is not bool
            or not isinstance(value['updated_by'], str)
            or not re.fullmatch(r'[A-Za-z0-9_\[\].-]{1,100}', value['updated_by'])):
        raise ValueError('Invalid CD policy.')
    for name in ('active', 'failure', 'last_success'):
        item = value[name]
        if item is None:
            continue
        allowed = {'run_id', 'attempt', 'commit', 'ci_number', 'mode', 'epoch', 'recovery_required', 'url'}
        if not isinstance(item, dict) or set(item) - allowed:
            raise ValueError('Invalid CD transaction.')
        if (not isinstance(item.get('run_id'), str) or not item['run_id'].isdigit()
                or not SHA.fullmatch(item.get('commit', ''))
                or type(item.get('ci_number')) is not int or item['ci_number'] < 1
                or item.get('mode') not in ENVIRONMENTS
                or type(item.get('epoch')) is not int or item['epoch'] < 0
                or type(item.get('attempt')) is not int or item['attempt'] < 1):
            raise ValueError('Invalid CD transaction identity.')
        if 'recovery_required' in item and type(item['recovery_required']) is not bool:
            raise ValueError('Invalid recovery state.')
        if 'url' in item and (not isinstance(item['url'], str) or not re.fullmatch(
                r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+', item['url'])):
            raise ValueError('Invalid run link.')
    return value


class Store:
    def __init__(self, api):
        self.api = api

    def load(self):
        try:
            document = self.api.request('GET', 'contents/' + FILE + '?ref=' + BRANCH)
        except ApiError as error:
            if error.status == 404:
                return initial(), None
            raise
        if (document.get('type') != 'file' or document.get('encoding') != 'base64'
                or not SHA.fullmatch(document.get('sha', ''))):
            raise ValueError('Invalid CD configuration envelope.')
        raw = base64.b64decode(''.join(document['content'].split()), validate=True)
        if len(raw) > 32768:
            raise ValueError('CD configuration exceeds its budget.')
        return validate(json.loads(raw)), document['sha']

    def update(self, operation, *, message):
        for retry in range(5):
            current, sha = self.load()
            if sha is None:
                raise ValueError('CD configuration branch is not initialized; keep Manual until setup completes.')
            value, result = operation(copy.deepcopy(current))
            validate(value)
            if value == current:
                return result
            body = {'branch': BRANCH, 'sha': sha, 'message': message,
                    'content': base64.b64encode((json.dumps(value, indent=2) + '\n').encode()).decode()}
            try:
                self.api.request('PUT', 'contents/' + FILE, body)
                return result
            except ApiError as error:
                if error.status not in (409, 422) or retry == 4:
                    raise
            except (OSError, TimeoutError):
                # A lost response may follow a committed write. Read back;
                # never issue a second ambiguous write without reconciliation.
                confirmed, _ = self.load()
                if confirmed == value:
                    return result
                raise RuntimeError('Configuration write was not confirmed; inspect the saved state.') from None
        raise RuntimeError('CD configuration changed repeatedly; no write was confirmed.')


def set_mode(store, mode, *, actor, control_number):
    if mode not in ENVIRONMENTS or type(control_number) is not int or control_number < 1:
        raise ValueError('Invalid mode selection.')
    def change(state):
        if control_number <= state['last_control']:
            return state, {'applied': False, 'reason': 'A newer control request has already been saved.', 'mode': state['mode']}
        if mode == 'auto' and (state['blocked'] or state['active'] and state['active'].get('recovery_required')):
            raise ValueError('Auto is blocked by a failed release. Select Manual and verify recovery/a successful manual release first.')
        if mode == 'auto' and state['active']:
            run = store.api.request('GET', 'actions/runs/' + state['active']['run_id'])
            if run.get('status') not in {'in_progress', 'queued', 'waiting', 'requested', 'pending'}:
                raise ValueError('The prior run ended without confirming its ownership release. Verify recovery first.')
        if mode != state['mode']:
            state['mode'] = mode
            state['epoch'] += 1
        state.update(last_control=control_number, updated_by=actor)
        return state, {'applied': True, 'mode': state['mode'], 'epoch': state['epoch'],
                       'blocked': state['blocked'], 'active_run': (state['active'] or {}).get('run_id'),
                       'notice': 'Saved for future releases. Existing review requests are not approved or cancelled.'}
    return store.update(change, message='Set persistent CD mode to ' + mode)


def begin(store, transaction):
    def claim(state):
        if state['mode'] != transaction['mode'] or state['epoch'] != transaction['epoch']:
            return state, {'began': False, 'reason': 'Mode changed before deployment; this release was skipped.'}
        if state['blocked'] and transaction['mode'] == 'auto':
            raise ValueError('Automatic delivery is blocked by a prior failure.')
        if state['active'] is not None:
            raise ValueError('A prior delivery owns the transaction. Inspect recovery before another release.')
        previous = state['last_success']
        if previous and transaction['ci_number'] < previous['ci_number']:
            return state, {'began': False, 'reason': 'A newer backend CI release is already deployed.'}
        state.update(active=copy.deepcopy(transaction), updated_by='delivery')
        return state, {'began': True, 'mode': transaction['mode']}
    return store.update(claim, message='Claim deployment ' + transaction['run_id'])


def closed_receipt(receipt, transaction):
    return (isinstance(receipt, dict) and receipt.get('commit') == transaction['commit']
            and receipt.get('guard_held') is False and receipt.get('recovery_required') is False
            and (receipt.get('completed') is True or receipt.get('rolled_back') is True
                 or receipt.get('preflight_only') is True and receipt.get('started') is False and receipt.get('patched') == []))


def finish(store, transaction, outcome, receipt=None, *, began=False):
    def complete(state):
        order = lambda item: (item['ci_number'], int(item['run_id']), item['attempt'])
        if any(item and order(item) > order(transaction) for item in (state['last_success'], state['failure'])):
            return state, {'outcome': 'stale outcome ignored', 'blocked': state['blocked']}
        active = state['active']
        owned = active is not None and all(active.get(k) == transaction[k] for k in ('run_id', 'attempt', 'commit'))
        if not owned and not began and (state['mode'] != transaction['mode'] or state['epoch'] != transaction['epoch']):
            return state, {'outcome': 'obsolete unstarted release skipped', 'blocked': state['blocked']}
        if active is not None and not owned:
            raise ValueError('Another release owns the transaction; outcome was not applied.')
        if outcome == 'success' and not began and not owned:
            return state, {'outcome': 'skipped', 'blocked': state['blocked']}
        safe = closed_receipt(receipt, transaction)
        successful = outcome == 'success' and safe and receipt.get('completed') is True
        if owned and safe:
            state['active'] = None
        if successful:
            previous = state['last_success']
            if previous is None or transaction['ci_number'] >= previous['ci_number']:
                state['last_success'] = copy.deepcopy(transaction)
            # A real approved Manual deployment is the acknowledgement that
            # clears the failure latch. Changing the checkbox cannot clear it.
            if transaction['mode'] == 'manual':
                state.update(blocked=False, failure=None)
        else:
            state['blocked'] = True
            state['failure'] = {**transaction, 'recovery_required': bool(owned and not safe),
                                'url': 'https://github.com/' + store.api.repository + '/actions/runs/' + transaction['run_id']}
            if owned and not safe:
                state['active']['recovery_required'] = True
        state['updated_by'] = 'delivery-result'
        return state, {'outcome': 'success' if successful else 'failed', 'blocked': state['blocked'],
                       'recovery_required': bool(state['active'])}
    return store.update(complete, message='Record deployment outcome ' + transaction['run_id'])


def read_receipt(directory, transaction):
    name = 'delivery-' + transaction['commit'] + '-' + transaction['run_id'] + '-' + str(transaction['attempt']) + '.json'
    path = Path(directory) / name
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        return None
    return json.loads(path.read_text(encoding='utf-8-sig'))


def emit(value):
    if 'RKE2_CD_ENABLED' in os.environ:
        value = {**value, 'cd_enabled': os.environ['RKE2_CD_ENABLED'] == 'true'}
    print(json.dumps(value))
    if os.environ.get('GITHUB_OUTPUT'):
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
            for name, item in value.items():
                if isinstance(item, (str, bool, int)):
                    stream.write(name + '=' + (json.dumps(item) if not isinstance(item, str) else item.replace('\n', ' ')) + '\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
            stream.write('### CD control\n\n' + '\n'.join('- **' + k + ':** ' + str(v) for k, v in value.items()) + '\n')


def initialize(api):
    repository = api.request('GET', '')
    if repository['default_branch'] != 'master':
        raise ValueError('This configuration currently targets master only.')
    try:
        api.request('GET', 'git/ref/heads/' + BRANCH)
    except ApiError as error:
        if error.status != 404:
            raise
        source = api.request('GET', 'git/ref/heads/master')
        api.request('POST', 'git/refs', {'ref': 'refs/heads/' + BRANCH, 'sha': source['object']['sha']})
    store = Store(api)
    state, sha = store.load()
    if sha is None:
        api.request('PUT', 'contents/' + FILE, {'branch': BRANCH, 'message': 'Initialize Manual CD control',
                    'content': base64.b64encode((json.dumps(state, indent=2) + '\n').encode()).decode()})
    confirmed, _ = store.load()
    return {'mode': confirmed['mode'], 'initialized': True, 'existing_state_preserved': sha is not None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('mode', 'begin', 'finish', 'status', 'initialize'))
    parser.add_argument('--auto', choices=('true', 'false'))
    parser.add_argument('--receipt-directory', default='receipt')
    args = parser.parse_args()
    store = Store(GitHub(os.environ['GITHUB_REPOSITORY']))
    if args.action == 'initialize':
        emit(initialize(store.api))
    elif args.action == 'mode':
        if os.environ.get('GITHUB_REF') != 'refs/heads/master':
            raise ValueError('CD Control may run only from master.')
        emit(set_mode(store, 'auto' if args.auto == 'true' else 'manual', actor=os.environ['GITHUB_ACTOR'],
                      control_number=int(os.environ['GITHUB_RUN_ID'])))
    elif args.action == 'status':
        state, _ = store.load()
        emit({'mode': state['mode'], 'blocked': state['blocked'], 'active_run': (state['active'] or {}).get('run_id')})
    else:
        transaction = {'run_id': os.environ['GITHUB_RUN_ID'], 'attempt': int(os.environ['GITHUB_RUN_ATTEMPT']),
                       'commit': os.environ['EXPECTED_COMMIT'], 'ci_number': int(os.environ['CI_NUMBER']),
                       'mode': os.environ['DELIVERY_MODE'], 'epoch': int(os.environ['CONTROL_EPOCH'])}
        emit(begin(store, transaction) if args.action == 'begin' else finish(
            store, transaction, os.environ['DELIVERY_RESULT'], read_receipt(args.receipt_directory, transaction),
            began=os.environ.get('DELIVERY_BEGAN') == 'true'))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never echo an API body, token or unexpected exception text.
        print(json.dumps({'cd_control_failed': True, 'error_type': type(error).__name__,
                          'reason': str(error) if isinstance(error, (ValueError, ApiError)) else 'Details omitted.'}))
        raise SystemExit(1)
