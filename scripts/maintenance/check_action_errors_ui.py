"""Failed actions and sign-in problems must be explained on screen (synthetic HTTP only).

Covers: Activate / region switch / user update rejected by the API, the region
list failing right after a successful sign-in, validation errors from the API,
and training again after a run completed. No production request is made.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
EDGE = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'


def main():
    state = {'me': 200, 'regions': 200, 'run_status': 'queued'}
    trains, errors, cases = [], [], []

    def error(message, code='synthetic'):
        return {'error': {'code': code, 'message': message}}

    def body_for(method, path):
        path = path.split('?')[0]
        if path.endswith('/training-monitor/capabilities'):
            return 200, {'cloud_training': True, 'local_training': False}
        if path.endswith('/auth/me'):
            if state['me'] == 401:
                return 401, error('Access token has expired.')
            if state['me'] == 503:
                return 503, {}
            return 200, {'id': 'admin', 'email': 'synthetic@example.test', 'role': 'admin', 'is_active': True}
        if path.endswith('/auth/password'):
            return 422, {'detail': [{'type': 'string_too_short', 'loc': ['body', 'new_password'],
                                     'msg': 'String should have at least 8 characters'}]}
        if path.endswith('/admin/regions') and method == 'GET':
            if state['regions'] == 503:
                return 503, error('Synthetic region service unavailable')
            return 200, {'items': [{'id': 'region-a', 'name': 'Region A', 'timezone': 'UTC', 'is_active': True,
                                    'active_model_version_id': 'model-1', 'active_model_version': 'model_v1',
                                    'active_model_variant': 'RandomForest'}]}
        if path.endswith('/regions/region-a') and method == 'PATCH':
            return 409, error('Synthetic: this region cannot be disabled right now.')
        if '/model-versions/' in path and path.endswith('/activate'):
            return 409, error('Synthetic: benchmark-only models cannot be activated.')
        if path.endswith('/admin/users'):
            return 200, {'items': [{'id': 'u1', 'email': 'only-admin@example.test', 'role': 'admin',
                                    'is_active': True, 'last_login_at': None}]}
        if method == 'PATCH' and '/admin/users/' in path:
            return 409, error('At least one active administrator must remain.', 'last_admin_required')
        if method == 'POST' and path.endswith('/training-runs'):
            trains.append(path)
            return 202, {'training_run': {'id': 'run-1', 'status': 'queued', 'dataset_id': 'dataset-a'}}
        if '/training-runs/' in path:
            return 200, {'id': 'run-1', 'status': state['run_status'], 'dataset_id': 'dataset-a'}
        if path.endswith('/datasets'):
            return 200, {'items': [{'id': 'dataset-a', 'region_id': 'region-a', 'original_filename': 'A.csv',
                                    'status': 'valid', 'row_count': 1000, 'start_at': '2026-01-01T00:00:00Z',
                                    'end_at': '2026-06-01T00:00:00Z'}]}
        if path.endswith('/model-versions'):
            return 200, {'items': [
                {'id': 'model-1', 'region_id': 'region-a', 'version': 'model_v1', 'variant': 'RandomForest',
                 'status': 'active', 'cv_mean_mae': 1, 'final_test_mae': 2, 'created_at': '2026-01-02T00:00:00Z'},
                {'id': 'model-2', 'region_id': 'region-a', 'version': 'model_v2', 'variant': 'LSTM',
                 'status': 'candidate', 'cv_mean_mae': 1, 'final_test_mae': 2, 'created_at': '2026-01-03T00:00:00Z'}]}
        if path.endswith('/drift-settings'):
            return 200, {'region_id': 'region-a', 'auto_retrain': False}
        if path.endswith('/forecast-dashboard'):
            return 200, {'hourly_24h': [], 'daily_7d': []}
        return 200, {'items': []}

    def respond(route):
        request = route.request
        path = urlparse(request.url).path
        if '/api/v1/' not in path:
            route.continue_() if urlparse(request.url).hostname == '127.0.0.1' else route.abort()
            return
        status, value = body_for(request.method, path + ('?' + urlparse(request.url).query if urlparse(request.url).query else ''))
        route.fulfill(status=status, content_type='application/json', body=json.dumps(value))

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT / 'frontend/dist')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=EDGE, headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            context.add_init_script("try{if(!sessionStorage.getItem('seeded')){localStorage.setItem('traffic_token','synthetic-only');sessionStorage.setItem('seeded','1');}}catch(e){}")
            context.route('**/*', respond)
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            url = f'http://127.0.0.1:{server.server_port}/'

            def tab(name):
                page.get_by_role('button', name=name, exact=True).click()

            def token():
                return page.evaluate("localStorage.getItem('traffic_token')")

            # A06: the region list fails right after a valid session is found.
            state['regions'] = 503
            page.goto(url)
            alert = page.get_by_role('alert').filter(has_text='Could not load the region list')
            expect(alert).to_be_visible()
            expect(alert).to_contain_text('Synthetic region service unavailable')
            expect(page.get_by_role('button', name='Logout', exact=True)).to_be_visible()
            assert token() == 'synthetic-only', 'a region list outage must not end the session'
            state['regions'] = 200
            alert.get_by_role('button', name='Retry', exact=True).click()
            expect(alert).to_have_count(0)
            expect(page.locator('.region-dropdown-trigger')).to_contain_text('Region A')
            cases.append('A06: region list outage keeps the session, shows the reason and Retry restores it')

            # A03: Activate rejected by the API.
            tab('Models')
            page.get_by_role('row').filter(has=page.get_by_role('cell', name='model_v2', exact=True)) \
                .get_by_role('button', name='Activate', exact=True).click()
            expect(page.get_by_role('alert').filter(has_text='Could not activate model_v2')) \
                .to_contain_text('benchmark-only models cannot be activated')
            cases.append('A03: Activate rejection is shown with its reason')

            # A03: region switch rejected.
            tab('Regions')
            page.get_by_title('Disable region').locator('input').click(force=True)
            expect(page.get_by_role('alert').filter(has_text='Could not disable Region A')) \
                .to_contain_text('cannot be disabled right now')
            expect(page.get_by_title('Disable region').locator('input')).to_be_checked()
            cases.append('A03: region switch rejection is shown and the switch returns to its real state')

            # A03: user update rejected (last admin).
            tab('Users')
            page.get_by_role('button', name='Toggle role', exact=True).click()
            expect(page.get_by_text('Could not update only-admin@example.test', exact=False)) \
                .to_contain_text('At least one active administrator must remain.')
            cases.append('A03: last-admin rejection is explained in User Management')

            # A08: validation errors are readable.
            page.locator('.profile-card').click()
            page.get_by_role('button', name='Change password', exact=True).click()
            dialog = page.locator('.modal-card')
            dialog.get_by_label('Current password', exact=True).fill('old-pass-123')
            dialog.get_by_label('New password', exact=True).fill('x')
            dialog.get_by_label('Confirm new password', exact=True).fill('x')
            dialog.get_by_role('button', name='Update password', exact=True).click()
            expect(dialog).to_contain_text('New password: String should have at least 8 characters')
            assert '[object Object]' not in dialog.inner_text()
            cases.append('A08: validation error is readable, not [object Object]')
            dialog.get_by_role('button', name='Close', exact=True).click()

            # A09: train again after a completed run.
            tab('Datasets')
            page.get_by_role('button', name='Train', exact=True).click()
            page.get_by_role('button', name='Train 5 models on Kaggle', exact=True).click()
            expect(page.locator('.train-modal-card')).not_to_be_visible(timeout=10000)
            state['run_status'] = 'completed'
            again = page.get_by_role('button', name='Train again', exact=True)
            expect(again).to_be_visible(timeout=12000)
            expect(page.get_by_text('Model ready', exact=True)).to_be_visible()
            again.click()
            expect(page.locator('.train-modal-card')).to_be_visible()
            page.get_by_role('button', name='Train 5 models on Kaggle', exact=True).click()
            expect(page.locator('.train-modal-card')).not_to_be_visible(timeout=10000)
            assert len(trains) == 2, trains
            cases.append('A09: a completed run offers Train again and sends a second request')

            # A06: authentication failure vs temporary outage while restoring a session.
            state['me'] = 401
            page.reload()
            expect(page.get_by_role('alert').filter(has_text='Access token has expired.')).to_be_visible()
            assert token() is None, 'an expired token should be removed'
            cases.append('A06: expired session returns to Login with the reason and clears the token')
            page.evaluate("localStorage.setItem('traffic_token','synthetic-only')")
            state['me'] = 503
            page.reload()
            expect(page.get_by_role('alert').filter(has_text='Could not sign you in right now')).to_be_visible()
            assert token() == 'synthetic-only', 'a temporary outage must keep the token'
            cases.append('A06: temporary outage shows a message and keeps the token')

            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
    print(json.dumps({'cases': cases, 'page_errors': errors}, indent=2))


if __name__ == '__main__':
    main()
