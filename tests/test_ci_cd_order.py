"""CD may consume only its inspected, completed and successful CI attempt."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.cicd import github_gate as gate
from tests import test_cicd as gate_tests

COMMIT = gate_tests.COMMIT

ROOT = Path(__file__).resolve().parents[1]


class CICDOrderingTests(unittest.TestCase):
    def choice(self):
        return {'run_id': '1', 'sha': COMMIT, 'ci_attempt': '1'}

    def test_nonterminal_ci_is_rejected_even_with_a_stale_success_conclusion(self):
        for status in ('queued', 'requested', 'waiting', 'in_progress', 'pending'):
            values = gate_tests.GateTests().responses()
            values[0]['status'] = status
            coverage = Mock()
            with self.subTest(status=status), self.assertRaises(ValueError):
                gate.validate('owner/repo', '1', fetch=Mock(side_effect=values), coverage=coverage)
            coverage.assert_not_called()

    def test_completion_event_must_match_commit_and_attempt_before_policy_or_coverage(self):
        for commit, attempt in (('b' * 40, 1), (COMMIT, 2), (COMMIT, 0), (COMMIT, True)):
            values = gate_tests.GateTests().responses()
            fetch, coverage = Mock(side_effect=values), Mock()
            with self.subTest(commit=commit, attempt=attempt), self.assertRaises(ValueError):
                gate.validate('owner/repo', '1', fetch=fetch, coverage=coverage,
                              expected_commit=commit, expected_attempt=attempt)
            self.assertEqual(fetch.call_count, 3)
            coverage.assert_not_called()

    def test_successful_matching_attempt_still_requires_the_full_coverage_gate(self):
        coverage = Mock()
        result = gate.validate('owner/repo', '1', fetch=Mock(side_effect=gate_tests.GateTests().responses()), coverage=coverage,
                               expected_commit=COMMIT, expected_attempt=1)
        self.assertEqual(result['ci_attempt'], '1')
        coverage.assert_called_once()
        gate.recheck_source('owner/repo', result, fetch=Mock(return_value=gate_tests.GateTests().responses()[0]))

    def test_rerun_after_inspection_invalidates_the_old_attempt_even_if_new_attempt_succeeds(self):
        inspected = gate.validate('owner/repo', '1', fetch=Mock(side_effect=gate_tests.GateTests().responses()), coverage=Mock())
        for status, conclusion in (('in_progress', None), ('queued', None), ('completed', 'success'), ('completed', 'failure')):
            rerun = {**gate_tests.GateTests().responses()[0], 'run_attempt': 2, 'status': status, 'conclusion': conclusion}
            with self.subTest(status=status, conclusion=conclusion), self.assertRaises(ValueError):
                gate.recheck_source('owner/repo', inspected, fetch=Mock(return_value=rerun))

    def test_recheck_rejects_wrong_run_commit_repository_or_unsuccessful_status(self):
        for key, value in (('id', 2), ('head_sha', 'b' * 40), ('head_repository', {'full_name': 'other/repo'}),
                           ('status', 'in_progress'), ('conclusion', 'cancelled'), ('event', 'pull_request')):
            run = {**gate_tests.GateTests().responses()[0], key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                gate.recheck_source('owner/repo', self.choice(), fetch=Mock(return_value=run))

    def test_preclaim_mode_only_reads_ci_and_does_not_overwrite_claim_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'outputs'
            output.write_text('preserve=value\n', encoding='utf-8')
            env = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_OUTPUT': str(output)}
            args = ['github_gate.py', '--run-id', '1', '--expected-commit', COMMIT, '--expected-attempt', '1', '--check-only']
            with patch.dict(os.environ, env, clear=True), patch('sys.argv', args), redirect_stdout(io.StringIO()), \
                 patch.object(gate, 'validate') as validate, patch.object(gate, 'recheck_source') as recheck:
                gate.main()
            validate.assert_not_called()  # Initial full coverage/policy stays in inspect; this mode only rereads CI.
            recheck.assert_called_once_with('owner/repo', self.choice())
            self.assertEqual(output.read_text(encoding='utf-8'), 'preserve=value\n')

    def test_preclaim_requires_the_identity_that_inspect_selected(self):
        for arguments in ([], ['--expected-commit', COMMIT], ['--expected-commit', COMMIT, '--expected-attempt', '0']):
            with self.subTest(arguments=arguments), patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}, clear=True), \
                 patch('sys.argv', ['github_gate.py', '--run-id', '1', '--check-only', *arguments]), \
                 patch.object(gate, 'recheck_source') as recheck:
                with self.assertRaises(ValueError): gate.main()
                recheck.assert_not_called()

    def test_automatic_event_without_source_attempt_cannot_select_a_deployment(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_EVENT_NAME': 'workflow_run'}, clear=True), \
             patch('sys.argv', ['github_gate.py', '--run-id', '1']), patch.object(gate, 'validate') as validate:
            with self.assertRaises(ValueError): gate.main()
            validate.assert_not_called()

    def test_initial_inspect_does_not_publish_outputs_if_ci_changes_after_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'outputs'
            env = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_OUTPUT': str(output),
                   'GITHUB_EVENT_NAME': 'workflow_run', 'CI_TRIGGER_SHA': COMMIT, 'CI_TRIGGER_ATTEMPT': '1'}
            with patch.dict(os.environ, env, clear=True), patch('sys.argv', ['github_gate.py', '--run-id', '1']), \
                 patch.object(gate, 'validate', return_value=self.choice()), \
                 patch.object(gate, 'recheck_source', side_effect=ValueError('CI rerun started')):
                with self.assertRaises(ValueError): gate.main()
            self.assertFalse(output.exists())

    def test_windows_preclaim_get_uses_the_existing_trusted_root_bundle(self):
        context = Mock()
        with patch.object(gate.os, 'name', 'nt'), patch.dict(os.environ, {'GH_TOKEN': 'SYNTHETIC_TOKEN', 'NODE_EXTRA_CA_CERTS': 'public-roots.pem'}), \
             patch.object(gate.ssl, 'create_default_context', return_value=context), \
             patch.object(gate, 'urlopen', return_value=io.BytesIO(b'{"id":1}')) as request:
            self.assertEqual(gate.get('repos/owner/repo/actions/runs/1'), {'id': 1})
        context.load_verify_locations.assert_called_once_with(cafile='public-roots.pem')
        self.assertIs(request.call_args.kwargs['context'], context)

    def test_workflow_waits_for_completed_success_and_rechecks_before_claim(self):
        text = (ROOT / '.github/workflows/backend-deploy.yml').read_text(encoding='utf-8')
        self.assertIn('types: [completed]', text)
        inspect = text.split('\n  inspect:\n')[1].split('\n  deploy:\n')[0]
        self.assertIn("github.event.workflow_run.status == 'completed'", inspect)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", inspect)
        self.assertIn('CI_TRIGGER_ATTEMPT: ${{ github.event.workflow_run.run_attempt }}', inspect)
        self.assertIn('CI_TRIGGER_SHA: ${{ github.event.workflow_run.head_sha }}', inspect)
        deploy = text.split('\n  deploy:\n')[1].split('\n  record-result:\n')[0]
        self.assertIn('needs: inspect', deploy)
        claim = deploy.split('id: claim')[1].split('\n      - name:')[0]
        self.assertIn('SOURCE_CI_ATTEMPT: ${{ needs.inspect.outputs.ci_attempt }}', claim)
        self.assertIn('--expected-commit $env:EXPECTED_COMMIT --expected-attempt $env:SOURCE_CI_ATTEMPT --check-only', claim)
        self.assertLess(claim.index('github_gate.py'), claim.index('cd_state.py begin'))
        self.assertIn('if ($LASTEXITCODE -ne 0)', claim[claim.index('github_gate.py'):claim.index('cd_state.py begin')])


if __name__ == '__main__':
    unittest.main()
