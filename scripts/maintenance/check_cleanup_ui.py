"""Browser regressions against synthetic HTTP only; no production requests."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / 'results/cleanup-improvements-20261119'


def main():
    state = {'status': 'not_started', 'phase': 'preparing', 'fail': False, 'deleted': False}
    writes, held, errors, cases = [], [], [], []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass

    def fulfill(route, value, status=200):
        route.fulfill(status=status, content_type='application/json', body=json.dumps(value))

    def respond(route):
        request = route.request
        path = urlparse(request.url).path
        if '/api/v1/' not in path:
            route.continue_() if urlparse(request.url).hostname == '127.0.0.1' else route.abort()
            return
        if request.method == 'DELETE':
            writes.append(path)
            if state['fail']:
                state.update(status='retry_required', phase='objects')
                fulfill(route, {'error': {'code': 'model_cleanup_incomplete', 'message': 'Synthetic storage unavailable'}}, 503)
            else:
                state.update(status='pending', phase='local_files')
                held.append(route)
            return
        if path.endswith('/auth/me'):
            body = {'id': 'admin', 'email': 'synthetic@example.test', 'role': 'admin', 'is_active': True}
        elif path.endswith('/cleanup-status'):
            body = {'state': state['status'], 'phase': state['phase'], 'done': ['local_files'] if state['phase'] != 'preparing' else [],
                    'can_retry': state['status'] in {'retry_required', 'not_started'}}
        elif path.endswith('/regions'):
            body = {'items': [{'id': 'region-a', 'name': 'Synthetic region', 'timezone': 'UTC', 'is_active': True,
                               'active_model_version_id': None}]}
        elif path.endswith('/model-versions'):
            body = {'items': [] if state['deleted'] else [{'id': 'model-a', 'version': 'model_v1', 'variant': 'GRU',
                    'status': 'candidate', 'cv_mean_mae': 1, 'final_test_mae': 2}]}
        elif path.endswith('/drift-settings'):
            body = {'region_id': 'region-a', 'auto_retrain': False}
        elif path.endswith('/forecast-dashboard'):
            body = {'hourly_24h': [], 'daily_7d': []}
        elif path.endswith('/datasets'):
            body = {'items': [{'id': 'dataset-a', 'original_filename': 'synthetic.csv', 'status': 'valid', 'row_count': 1000,
                'start_date': '2026-01-01', 'end_date': '2026-11-01'}]}
        else:
            body = {'items': []}
        fulfill(route, body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT / 'frontend/dist')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', headless=True)
            context = browser.new_context(viewport={'width':1440, 'height':1000})
            context.add_init_script("localStorage.setItem('traffic_token','synthetic-only')")
            context.route('**/*', respond)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            page.get_by_role('button',name='Models',exact=True).click()
            page.get_by_role('button',name='Delete',exact=True).click()
            dialog = page.get_by_role('alertdialog')
            dialog.get_by_role('button',name='Delete model',exact=True).click()
            expect(dialog.get_by_role('button',name='Deleting…',exact=True)).to_be_disabled()
            expect(dialog.get_by_role('button',name='Close',exact=True)).to_be_disabled()
            dialog.press('Escape')
            expect(dialog).to_be_visible()
            state['phase']='objects'
            expect(dialog.get_by_text('Deleting stored files',exact=True)).to_be_visible(timeout=12000)
            assert len(writes)==1
            OUTPUT.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(OUTPUT/'model-progress-desktop.png'))
            cases.append('One DELETE; live server phase changes; Escape and repeat submission blocked')

            # F5 interrupts the HTTP client, not the server. The new page only
            # polls the persisted item and sends no new DELETE.
            page.reload()
            expect(page.get_by_role('alertdialog')).to_be_visible()
            expect(page.get_by_role('alertdialog').get_by_text('Deleting stored files',exact=True)).to_be_visible(timeout=12000)
            assert len(writes)==1
            state.update(status='completed',phase='completed',deleted=True)
            for route in held:
                try: route.abort()
                except Exception: pass
            held.clear()
            expect(page.get_by_role('alertdialog')).not_to_be_visible(timeout=12000)
            assert page.evaluate("sessionStorage.getItem('traffic_cleanup:admin:model')") is None
            cases.append('Reload restores saved progress; lost DELETE reply reconciled by GET; no duplicate write')

            state.update(status='not_started',phase='preparing',deleted=False,fail=True)
            page.reload()
            page.get_by_role('button',name='Delete',exact=True).click()
            page.get_by_role('alertdialog').get_by_role('button',name='Delete model',exact=True).click()
            retry=page.get_by_role('alertdialog').get_by_role('button',name='Retry deletion',exact=True)
            expect(retry).to_be_visible(timeout=12000)
            assert len(writes)==2
            page.set_viewport_size({'width':390,'height':844})
            box=page.get_by_role('alertdialog').bounding_box()
            assert box['width']<=390 and box['height']<=844
            page.screenshot(path=str(OUTPUT/'retry-mobile.png'))
            state['fail']=False
            retry.click()
            expect(page.get_by_role('alertdialog').get_by_role('button',name='Deleting…',exact=True)).to_be_disabled()
            assert len(writes)==3
            state.update(status='completed',phase='completed',deleted=True)
            for route in held: route.fulfill(status=204,body='')
            held.clear()
            expect(page.get_by_role('alertdialog')).not_to_be_visible(timeout=12000)
            cases.append('Failed cleanup exposes explicit Retry; retry sends exactly one new DELETE; fits mobile')

            page.set_viewport_size({'width':1440,'height':1000})
            page.get_by_role('button',name='Datasets',exact=True).click()
            page.get_by_role('button',name='Advanced',exact=True).click()
            page.get_by_role('button',name='Expand training configuration',exact=True).click()
            config=page.get_by_role('dialog')
            config.get_by_label('CV folds',exact=True).fill('4')
            config.get_by_role('button',name='Done',exact=True).click()
            page.get_by_role('button',name='Expand training configuration',exact=True).click()
            expect(page.get_by_role('dialog').get_by_label('CV folds',exact=True)).to_have_value('4')
            expect(page.get_by_role('dialog').get_by_label('End date (excluded)',exact=True)).to_be_visible()
            cases.append('Expanded training settings preserve edits after close and label exclusive end date')
            assert not errors, errors
            (OUTPUT/'browser-proof.json').write_text(json.dumps({'cases':cases,'delete_count':len(writes),'page_errors':errors},indent=2),encoding='utf-8')
            browser.close()
    finally:
        server.shutdown()
    print(json.dumps({'cases':cases,'delete_count':len(writes),'page_errors':errors}))


if __name__=='__main__': main()
