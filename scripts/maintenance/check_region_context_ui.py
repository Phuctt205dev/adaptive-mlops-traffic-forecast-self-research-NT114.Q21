"""Region-context regressions for the built admin UI, using synthetic HTTP only.

Delayed answers for one region must never replace what is shown for the region
the admin is looking at (datasets, models, prediction window/result, dashboard).
No production request is made; every /api/v1 call is answered by this script.
"""
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
EDGE = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'

REGIONS = {
    'region-a': {'name': 'Region A', 'model': 'model-a1', 'version': 'model_v1', 'start': '2026-01-03'},
    'region-b': {'name': 'Region B', 'model': 'model-b1', 'version': 'model_v9', 'start': '2026-01-08'},
    'region-c': {'name': 'Region C', 'model': None, 'version': None, 'start': None},
}


def main():
    state = {'hold': set(), 'active_a': 'model-a1'}
    held, trains, calls, errors, cases = [], [], [], [], []

    def region_items():
        items = []
        for region_id, info in REGIONS.items():
            active = state['active_a'] if region_id == 'region-a' else info['model']
            items.append({
                'id': region_id, 'name': info['name'], 'timezone': 'UTC', 'is_active': True,
                'active_model_version_id': active,
                'active_model_version': 'model_v2' if active == 'model-a2' else info['version'],
                'active_model_variant': 'RandomForest' if active else None,
            })
        return items

    def window_start(region_id):
        if region_id == 'region-a' and state['active_a'] == 'model-a2':
            return '2026-01-05'
        return REGIONS[region_id]['start']

    def body_for(method, path, request):
        parts = path.split('/')
        if path.endswith('/training-monitor/capabilities'):
            return {'cloud_training': True, 'local_training': False}
        if path.endswith('/auth/me'):
            return {'id': 'admin', 'email': 'synthetic@example.test', 'role': 'admin', 'is_active': True}
        if path.endswith('/regions'):
            return {'items': region_items()}
        if method == 'POST' and '/model-versions/' in path and path.endswith('/activate'):
            state['active_a'] = 'model-a2'
            return {}
        if method == 'POST' and path.endswith('/training-runs'):
            trains.append(path)
            return {'training_run': {'id': 'run-1', 'status': 'queued', 'dataset_id': parts[-2]}}
        if '/training-runs/' in path:
            return {'id': 'run-1', 'status': 'queued', 'dataset_id': 'x'}
        if path.endswith('/datasets'):
            region_id = parts[-2]
            letter = region_id[-1]
            return {'items': [{
                'id': f'dataset-{letter}', 'region_id': region_id, 'original_filename': f'{letter.upper()}.csv',
                'status': 'valid', 'row_count': 1000, 'start_at': '2026-01-01T00:00:00Z', 'end_at': '2026-06-01T00:00:00Z',
            }]}
        if path.endswith('/model-versions'):
            region_id = parts[-2]
            version = REGIONS[region_id]['version'] or 'model_v0'
            items = [{
                'id': REGIONS[region_id]['model'] or 'model-c1', 'region_id': region_id, 'version': version,
                'variant': 'RandomForest', 'status': 'candidate', 'cv_mean_mae': 1, 'final_test_mae': 2,
                'created_at': '2026-01-02T00:00:00Z',
            }]
            if region_id == 'region-a':
                items.append({**items[0], 'id': 'model-a2', 'version': 'model_v2'})
            return {'items': items}
        if path.endswith('/prediction-window'):
            start = window_start(parts[-2])
            return {'production_start_at': f'{start}T00:00:00Z', 'production_end_at': '2026-03-01T00:00:00Z'}
        if path.endswith('/forecast-dashboard'):
            return {'hourly_24h': [{'forecast_for': '2026-11-22T10:00:00Z', 'prediction': 321}], 'daily_7d': []}
        if path.endswith('/drift-settings'):
            return {'region_id': parts[-2], 'auto_retrain': False}
        return {'items': []}

    def fulfill(route, value):
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    def respond(route):
        request = route.request
        path = urlparse(request.url).path
        if '/api/v1/' not in path:
            route.continue_() if urlparse(request.url).hostname == '127.0.0.1' else route.abort()
            return
        calls.append(path)
        value = body_for(request.method, path, request)
        key = next((name for name in state['hold'] if name in path), None)
        if key:
            held.append((route, value))
            return
        fulfill(route, value)

    def release():
        state['hold'].clear()
        pending = list(held)
        held.clear()
        for route, value in pending:
            fulfill(route, value)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT / 'frontend/dist')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=EDGE, headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            context.add_init_script(
                "localStorage.setItem('traffic_token','synthetic-only');"
                "localStorage.setItem('traffic_region_admin','region-a');")
            context.route('**/*', respond)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            url = f'http://127.0.0.1:{server.server_port}/'

            def tab(name):
                page.get_by_role('button', name=name, exact=True).click()

            def switch(name):
                page.locator('.region-dropdown-trigger').click()
                page.get_by_role('option', name=name, exact=False).click()

            def count(suffix):
                return sum(call.endswith(suffix) for call in calls)

            # A01: answers for region A arrive after the admin moved to region B.
            state['hold'] = {'/regions/region-a/datasets', '/regions/region-a/model-versions'}
            page.goto(url)
            tab('Datasets')
            expect(page.locator('.region-dropdown-trigger')).to_contain_text('Region A')
            expect(page.get_by_text('Loading datasets…', exact=True)).to_be_visible()
            switch('Region B')
            expect(page.get_by_role('cell', name='B.csv', exact=True)).to_be_visible()
            release()
            page.wait_for_timeout(300)
            expect(page.get_by_role('cell', name='B.csv', exact=True)).to_be_visible()
            expect(page.get_by_role('cell', name='A.csv', exact=True)).to_have_count(0)
            tab('Models')
            expect(page.get_by_role('cell', name='model_v9', exact=True)).to_be_visible()
            expect(page.get_by_role('cell', name='model_v1', exact=True)).to_have_count(0)
            tab('Datasets')
            page.get_by_role('button', name='Train', exact=True).click()
            page.get_by_role('button', name='Train 5 models on Kaggle', exact=True).click()
            expect(page.locator('.train-modal-card')).not_to_be_visible(timeout=10000)
            assert trains == ['/api/v1/admin/datasets/dataset-b/training-runs'], trains
            cases.append('A01: late region A datasets/models never replace region B; Train targets region B')

            # A01: the dataset list shows a loading text instead of the previous region's rows.
            state['hold'] = {'/regions/region-a/datasets'}
            switch('Region A')
            expect(page.get_by_role('cell', name='B.csv', exact=True)).to_have_count(0)
            expect(page.get_by_text('Loading datasets…', exact=True)).to_be_visible()
            release()
            expect(page.get_by_role('cell', name='A.csv', exact=True)).to_be_visible()
            cases.append('A01: switching region clears the previous region rows immediately')

            # A02: window follows the active model; late window answers are dropped.
            page.reload()
            tab('Prediction')
            field = page.get_by_label('Forecast time', exact=True)
            expect(field).to_have_attribute('min', '2026-01-03T00:00')
            field.fill('2026-01-03T08:00')
            before = count('/regions/region-a/prediction-window')
            tab('Models')
            page.get_by_role('row').filter(has=page.get_by_role('cell', name='model_v2', exact=True))                 .get_by_role('button', name='Activate', exact=True).click()
            tab('Prediction')
            expect(field).to_have_attribute('min', '2026-01-05T00:00')
            assert count('/regions/region-a/prediction-window') > before
            cases.append('A02: Activate in the same region reloads the prediction window')

            state['hold'] = {'/regions/region-b/prediction-window'}
            switch('Region B')
            expect(field).to_have_value('')
            switch('Region A')
            expect(field).to_have_attribute('min', '2026-01-05T00:00')
            release()
            page.wait_for_timeout(300)
            expect(page.locator('.region-dropdown-trigger')).to_contain_text('Region A')
            expect(field).to_have_attribute('min', '2026-01-05T00:00')
            cases.append('A02: a late window answer from another region does not change the form')

            # A10: Updating... must not stay after moving to a region without an active model.
            state['hold'] = {'/regions/region-a/forecast-dashboard'}
            page.reload()
            tab('Dashboard')
            expect(page.locator('.region-dropdown-trigger')).to_contain_text('Region A')
            expect(page.locator('.forecast-loading')).to_be_visible()
            switch('Region C')
            expect(page.locator('.forecast-loading')).to_have_count(0)
            release()
            page.wait_for_timeout(300)
            expect(page.locator('.forecast-loading')).to_have_count(0)
            expect(page.locator('.hour-card')).to_have_count(0)
            cases.append('A10: no stuck Updating and no stale forecast after switching to a region without a model')

            # A07: a new hour (or returning to the tab) requests a fresh forecast.
            page.clock.install(time=datetime(2026, 11, 22, 10, 5, tzinfo=timezone.utc))
            page.reload()
            tab('Dashboard')
            switch('Region B')
            expect(page.locator('.hour-card')).to_have_count(1)
            baseline = count('/regions/region-b/forecast-dashboard')
            page.clock.run_for('00:30:00')
            assert count('/regions/region-b/forecast-dashboard') == baseline, 'refreshed inside the same hour'
            page.clock.run_for('01:10:00')
            page.wait_for_timeout(300)
            assert count('/regions/region-b/forecast-dashboard') > baseline, 'no refresh after the hour changed'
            cases.append('A07: dashboard asks again after the hour changes, not within the same hour')

            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
    print(json.dumps({'cases': cases, 'page_errors': errors}, indent=2))


if __name__ == '__main__':
    main()
