"""Release outcomes must explain themselves, and read-only GitHub calls must tolerate blips.

These cover the delivery gate and scope rules only; they never touch a cluster.
"""
import email.message
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from scripts.cicd import ci_coverage, github_gate, pipeline, transient
from tests.test_application_delivery import A, B, delivery_identity, proof_for

ROOT = Path(__file__).resolve().parents[1]
UI_SCRIPT = 'scripts/maintenance/check_action_errors_ui.py'


def http_error(code, **headers):
    message = email.message.Message()
    for key, value in headers.items():
        message[key.replace('_', '-')] = value
    return HTTPError('https://api.github.com/x', code, 'x', message, None)


class Response:
    def __init__(self, value):
        self.raw = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=None):
        return self.raw


class UiCheckScopeTests(unittest.TestCase):
    def test_every_browser_check_script_in_the_repository_is_recognised(self):
        found = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / 'scripts').glob('*/check_*_ui.py'))
        self.assertIn(UI_SCRIPT, found)
        for path in found:
            with self.subTest(path=path):
                self.assertTrue(pipeline.ui_check_path(path))

    def test_only_single_level_browser_checks_are_exempt(self):
        for path in ('scripts/cicd/deploy.py', 'scripts/shap/new_rollout.py', 'scripts/maintenance/check_cleanup.py',
                     'scripts/a/b/check_x_ui.py', 'backend/app/check_x_ui.py', 'scripts/check_x_ui.py',
                     'scripts/cicd/check_x_ui.py.sh', 'scripts/maintenance/Check_X_ui.py'):
            with self.subTest(path=path):
                self.assertFalse(pipeline.ui_check_path(path))

    def test_exempt_scripts_are_never_part_of_a_packaged_role(self):
        from scripts.cicd.roles_release import closure
        found = {p.relative_to(ROOT).as_posix() for p in (ROOT / 'scripts').glob('*/check_*_ui.py')}
        for role in ('api', 'tree', 'neural', 'sync'):
            with self.subTest(role=role):
                self.assertFalse(found & set(closure(ROOT, role)))

    def test_ui_script_with_a_frontend_edit_is_a_pages_only_release(self):
        scope = pipeline.classify([UI_SCRIPT, 'frontend/src/main.jsx'], baseline=B, commit=A, frontend=True)
        self.assertEqual(scope['schema'], pipeline.SCOPE)
        self.assertTrue(scope['tests']);self.assertTrue(scope['frontend'])
        self.assertFalse(scope['backend']);self.assertFalse(scope['current']);self.assertFalse(scope['full'])
        pipeline.validate_scope(scope, A)
        self.assertFalse({'image', 'package'} & pipeline.required_results(scope))
        accepted = pipeline.check_acceptance(proof_for([UI_SCRIPT, 'frontend/src/main.jsx'], frontend=True),
                                             {'id': 1, 'head_sha': A, 'run_attempt': 2})
        self.assertFalse(accepted['backend'])

    def test_real_backend_or_unknown_script_still_builds_a_backend_release(self):
        for paths in ([UI_SCRIPT, 'backend/app/main.py'], ['scripts/shap/new_rollout.py'], ['scripts/maintenance/check_cleanup.py']):
            with self.subTest(paths=paths):
                self.assertTrue(pipeline.classify(paths, baseline=B, commit=A)['backend'])

    def test_a_scope_cannot_be_downgraded_by_hand(self):
        scope = pipeline.classify(['backend/app/main.py'], baseline=B, commit=A)
        with self.assertRaises(ValueError):
            pipeline.validate_scope({**scope, 'backend': False}, A)
        ui_only = pipeline.classify([UI_SCRIPT], baseline=B, commit=A)
        with self.assertRaises(ValueError):
            pipeline.validate_scope({**ui_only, 'backend': True}, A)

    def test_v4_proofs_made_before_the_pattern_stay_readable_and_unchanged(self):
        old = pipeline.classify_v4([UI_SCRIPT], baseline=B, commit=A)
        self.assertEqual(old['schema'], 'traffic-application-scope/v4')
        self.assertTrue(old['backend'], 'v4 treated this script as backend; it is frozen')
        pipeline.validate_scope(old, A)
        results = {n: {'result': 'success'} for n in pipeline.required_results(old)}
        accepted = pipeline.aggregate(old, {}, results, commit=A, run_id=1, attempt=2, identity=delivery_identity())
        self.assertEqual(accepted['scope']['schema'], 'traffic-application-scope/v4')
        pipeline.check_acceptance(accepted, {'id': 1, 'head_sha': A, 'run_attempt': 2, 'event': 'push'})
        self.assertEqual(pipeline.required_jobs(old), pipeline.required_jobs({**old, 'schema': pipeline.SCOPE}))

    def test_listed_ui_files_keep_their_old_classification_in_every_frozen_schema(self):
        for path in pipeline.UI_CHECK_FILES:
            for classify in (pipeline.classify_v2, pipeline.classify_v3, pipeline.classify_v4, pipeline.classify):
                with self.subTest(path=path, schema=classify.__name__):
                    self.assertFalse(classify([path], baseline=B, commit=A)['backend'])


class SupersedeTests(unittest.TestCase):
    @staticmethod
    def trees(old, new):
        return [{'default_branch': 'master'}, {'object': {'sha': B}}, {'tree': {'sha': 'o'}},
                {'truncated': False, 'tree': old}, {'tree': {'sha': 'n'}}, {'truncated': False, 'tree': new}]

    def test_ui_check_script_alone_does_not_supersede_a_backend_release(self):
        base = [{'path': 'backend/app/main.py', 'type': 'blob', 'sha': 's'}]
        extra = base + [{'path': UI_SCRIPT, 'type': 'blob', 'sha': 'new'}]
        details = {}
        self.assertTrue(pipeline.current_source('owner/repo', A, Mock(side_effect=self.trees(base, extra)), details))
        self.assertEqual(details, {})

    def test_a_real_backend_change_supersedes_and_names_the_newer_commit_and_files(self):
        base = [{'path': 'backend/app/main.py', 'type': 'blob', 'sha': 's'}]
        newer = [{'path': 'backend/app/main.py', 'type': 'blob', 'sha': 't'}, {'path': 'src/z.py', 'type': 'blob', 'sha': 'n'}]
        details = {}
        self.assertFalse(pipeline.current_source('owner/repo', A, Mock(side_effect=self.trees(base, newer)), details))
        self.assertEqual(details['latest'], B)
        self.assertEqual(details['changed'], ['backend/app/main.py', 'src/z.py'])
        self.assertEqual(details['changed_total'], 2)

    def test_long_file_lists_are_bounded_and_the_old_call_shape_still_works(self):
        base = []
        newer = [{'path': 'src/f%02d.py' % i, 'type': 'blob', 'sha': 'n'} for i in range(25)]
        details = {}
        self.assertFalse(pipeline.current_source('owner/repo', A, Mock(side_effect=self.trees(base, newer)), details))
        self.assertEqual(len(details['changed']), 10);self.assertEqual(details['changed_total'], 25)
        self.assertFalse(pipeline.current_source('owner/repo', A, Mock(side_effect=self.trees(base, newer))))

    def test_same_commit_needs_no_tree_reads(self):
        fetch = Mock(side_effect=[{'default_branch': 'master'}, {'object': {'sha': A}}])
        self.assertTrue(pipeline.current_source('owner/repo', A, fetch, {}))
        self.assertEqual(fetch.call_count, 2)


class Reporting(unittest.TestCase):
    def run_main(self, argv, **patches):
        with tempfile.TemporaryDirectory() as directory:
            out, summary = Path(directory) / 'out', Path(directory) / 'summary'
            env = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_OUTPUT': str(out), 'GITHUB_STEP_SUMMARY': str(summary)}
            printed = io.StringIO()
            error = None
            with patch.dict(os.environ, env, clear=True), patch('sys.argv', ['github_gate.py', *argv]), \
                    patch.object(github_gate, 'validate', **patches), \
                    patch.object(github_gate, 'recheck_source'), redirect_stdout(printed):
                try:
                    github_gate.main()
                except Exception as caught:
                    error = caught
            read = lambda p: p.read_text(encoding='utf-8') if p.exists() else ''
            return printed.getvalue(), read(out), read(summary), error

    def test_superseded_release_says_what_replaced_it_and_what_to_do(self):
        text = 'Newer master bbbbbbb changes release inputs (backend/app/main.py, src/z.py).'
        printed, out, summary, error = self.run_main(['--run-id', '1'], side_effect=github_gate.Superseded(text))
        self.assertIsNone(error)
        self.assertIn('eligible=false', out)
        self.assertIn('reason=Superseded before deployment: ' + text, out)
        self.assertIn('::notice title=Not deployed', printed)
        for expected in ('Not deployed', 'bbbbbbb', 'backend/app/main.py', 'Re-run all jobs'):
            self.assertIn(expected, summary)
        self.assertEqual(printed.count('::notice'), 1)

    def test_a_rejected_gate_explains_the_rule_and_still_fails(self):
        message = 'Auto is blocked by a failed release; use Manual to verify a repair.'
        printed, out, summary, error = self.run_main(['--run-id', '1'], side_effect=ValueError(message))
        self.assertIsInstance(error, ValueError)
        self.assertIn('::error title=Release gate rejected this run::', printed)
        self.assertIn(message, summary)
        self.assertIn('Re-running will not change', summary)
        self.assertNotIn('eligible', out)

    def test_a_temporary_github_failure_says_to_rerun_all_jobs(self):
        message = 'GitHub API check failed (HTTP 502); token/body omitted.'
        printed, out, summary, error = self.run_main(['--run-id', '1'], side_effect=RuntimeError(message))
        self.assertIsInstance(error, RuntimeError)
        self.assertIn('temporary', summary.lower())
        self.assertIn('Re-run all jobs', summary);self.assertIn('not "failed jobs only"', summary)

    def test_secrets_and_newlines_never_reach_annotations_or_the_summary(self):
        secret = 'ghp_' + 'a' * 36
        printed, out, summary, error = self.run_main(['--run-id', '1'],
                                                     side_effect=ValueError('bad\nline Bearer abc.def ' + secret))
        for text in (printed, summary):
            self.assertNotIn(secret, text);self.assertNotIn('abc.def', text)
        annotation = [l for l in printed.splitlines() if l.startswith('::error')]
        self.assertEqual(len(annotation), 1)
        self.assertIn('bad line', annotation[0])

    def test_long_messages_are_bounded(self):
        printed, out, summary, error = self.run_main(['--run-id', '1'], side_effect=ValueError('x' * 5000))
        self.assertLess(len(printed), 1200);self.assertLess(len(summary), 1600)

    def test_no_summary_file_is_fine(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}, clear=True), \
                patch('sys.argv', ['github_gate.py', '--run-id', '1']), \
                patch.object(github_gate, 'validate', side_effect=ValueError('rule')), \
                redirect_stdout(io.StringIO()) as printed, self.assertRaises(ValueError):
            github_gate.main()
        self.assertIn('::error', printed.getvalue())

    def test_precheck_superseded_reports_the_reason_too(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / 'summary'
            env = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_STEP_SUMMARY': str(summary)}
            with patch.dict(os.environ, env, clear=True), \
                    patch('sys.argv', ['github_gate.py', '--run-id', '1', '--expected-commit', A, '--expected-attempt', '2', '--check-only']), \
                    patch.object(github_gate, 'recheck_source', side_effect=github_gate.Superseded('Newer master ccccccc')), \
                    redirect_stdout(io.StringIO()) as printed:
                github_gate.main()
            value = json.loads(printed.getvalue().strip().splitlines()[-1])
            self.assertFalse(value['eligible'])
            self.assertIn('ccccccc', value['reason'])
            self.assertIn('ccccccc', summary.read_text(encoding='utf-8'))

    def test_gate_attaches_the_details_to_the_superseded_exception(self):
        from tests import test_cicd
        values = test_cicd.GateTests().responses()
        values[0].update(id=1, run_attempt=2, status='in_progress', conclusion=None)

        def fake_current(repository, commit, fetch, details=None):
            details.update(latest=B, changed=['src/z.py'], changed_total=1)
            return False
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'push', 'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_SHA': A}, clear=True), \
                patch.object(pipeline, 'current_source', side_effect=fake_current), \
                self.assertRaises(github_gate.Superseded) as raised:
            github_gate.validate('owner/repo', 1, fetch=Mock(side_effect=values), expected_commit=A, expected_attempt=2,
                                 current_run=True, coverage=Mock(return_value=proof_for(['backend/app/prediction.py'])))
        self.assertIn(B[:7], str(raised.exception));self.assertIn('src/z.py', str(raised.exception))


class RetryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(transient.time, 'sleep')
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_recovers_after_network_blips_with_short_bounded_waits(self):
        call = Mock(side_effect=[URLError('reset'), TimeoutError(), 'ok'])
        self.assertEqual(transient.retry(call), 'ok')
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2])

    def test_gives_up_after_three_attempts_with_the_last_error(self):
        call = Mock(side_effect=[http_error(503), http_error(502), http_error(500)])
        with self.assertRaises(HTTPError) as raised:
            transient.retry(call)
        self.assertEqual(raised.exception.code, 500);self.assertEqual(call.call_count, 3)

    def test_rules_and_permissions_are_never_retried(self):
        for error in (http_error(404), http_error(401), http_error(403), http_error(422), ValueError('rule')):
            call = Mock(side_effect=error)
            with self.subTest(error=repr(error)), self.assertRaises(type(error)):
                transient.retry(call)
            self.assertEqual(call.call_count, 1)
        self.sleep.assert_not_called()

    def test_rate_limits_are_retried_but_plain_403_is_not(self):
        self.assertTrue(transient.is_transient(http_error(429)))
        self.assertTrue(transient.is_transient(http_error(403, Retry_After='2')))
        self.assertFalse(transient.is_transient(http_error(403)))

    def test_gate_reads_survive_a_502_and_a_dropped_connection(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'x'}), \
                patch.object(github_gate, 'urlopen', side_effect=[http_error(502), URLError('reset'), Response({'id': 7})]):
            self.assertEqual(github_gate.get('repos/o/r'), {'id': 7})

    def test_gate_reads_keep_their_error_contract(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'secret-value'}):
            with patch.object(github_gate, 'urlopen', side_effect=http_error(404)), self.assertRaises(github_gate.MissingResource):
                github_gate.get('repos/o/r')
            with patch.object(github_gate, 'urlopen', side_effect=http_error(401)), \
                    self.assertRaisesRegex(RuntimeError, r'HTTP 401\); token/body omitted'):
                github_gate.get('repos/o/r')
            with patch.object(github_gate, 'urlopen', side_effect=[URLError('secret-value')] * 3), \
                    self.assertRaises(RuntimeError) as raised:
                github_gate.get('repos/o/r')
            self.assertIn('GitHub API check failed', str(raised.exception))
            self.assertNotIn('secret-value', str(raised.exception))

    def test_artifact_download_retries_but_keeps_its_size_and_envelope_checks(self):
        import io as _io, zipfile
        buffer = _io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('ci-acceptance.json', json.dumps({'ok': True}))
        reply = Mock();reply.__enter__ = Mock(return_value=reply);reply.__exit__ = Mock(return_value=False)
        reply.read.return_value = buffer.getvalue()
        opener = Mock();opener.open.side_effect = [URLError('reset'), reply]
        fetch = Mock(return_value={'artifacts': [{'name': 'proof', 'id': 5, 'expired': False}]})
        with patch.dict(os.environ, {'GH_TOKEN': 'x'}), patch.object(ci_coverage, 'build_opener', return_value=opener):
            self.assertEqual(ci_coverage.read_artifact('owner/repo', '1', 'proof', fetch), {'ok': True})
        self.assertEqual(opener.open.call_count, 2)


if __name__ == '__main__':
    unittest.main()
