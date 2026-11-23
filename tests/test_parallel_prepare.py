"""CI runs the regression tests beside the image builds without weakening any gate.

The images may build while the tests run, but nothing is pushed to the registry until the
same run attempt's `tests` job succeeded, and the release package still needs both.
"""
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import re
import unittest
from unittest.mock import Mock, patch

from scripts.cicd import benchmark, pipeline, wait_for_job

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / '.github/workflows/backend-ci.yml').read_text(encoding='utf-8')


def job(name):
    body = SOURCE.split('\njobs:\n', 1)[1]
    match = re.search(r'^  ' + re.escape(name) + r':\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)', body, re.M | re.S)
    assert match, name
    return match.group(1)


def needs(name):
    match = re.search(r'^    needs: (.+)$', job(name), re.M)
    return set(re.findall(r'[a-z][a-z0-9-]*', match.group(1))) if match else set()


def step(name, title):
    for part in re.split(r'\n      - ', job(name)):
        if part.startswith('name: ' + title):
            return part
    raise AssertionError(title)


def condition(name):
    match = re.search(r'^    if: (.*?)(?=^    [a-z-]+:)', job(name), re.M | re.S)
    return ' '.join(match.group(1).split()) if match else None


class WorkflowGraphTests(unittest.TestCase):
    def test_regression_tests_run_once_in_their_own_job(self):
        self.assertEqual(SOURCE.count('unittest discover'), 1)
        tests = job('tests')
        self.assertIn('python -B -m unittest discover -s tests -v', tests)
        # It keeps every check it ran before, including the release build and fixtures that also protect
        # pull requests; it only stops producing the artifacts and outputs the images consume.
        for kept in ('release.py Build --roles --directory release', 'components.py --directory release',
                     'role_checks.py fixtures --directory fixtures'):
            self.assertIn(kept, tests)
        self.assertLess(tests.index('unittest discover'), tests.index('release.py Build'))
        for forbidden in ('upload-artifact', 'outputs:', 'id: component-matrix'):
            self.assertNotIn(forbidden, tests)
        self.assertEqual(needs('tests'), {'scope'})
        self.assertIn("needs.scope.outputs.tests == 'true'", tests)

    def test_prepare_builds_the_checked_source_and_fixtures_without_the_regression(self):
        prepare = job('prepare')
        self.assertEqual(needs('prepare'), {'scope'})
        self.assertNotIn('unittest', prepare)
        for expected in ('release.py Build --roles --directory release', 'components.py --directory release',
                         'role_checks.py fixtures --directory fixtures',
                         'name: checked-source-${{ github.sha }}', 'name: offline-model-fixtures-${{ github.sha }}',
                         'steps.component-matrix.outputs.applications', 'steps.component-matrix.outputs.executors'):
            self.assertIn(expected, prepare)
        self.assertIn('applications:', prepare.split('steps:')[0])
        self.assertIn('executors:', prepare.split('steps:')[0])

    def test_prepare_has_the_same_environment_as_the_tests_it_used_to_share_a_job_with(self):
        def env(body):
            return re.search(r'    env:\n((?:      .+\n)+)', body).group(1)
        self.assertEqual(env(job('prepare')), env(job('tests')))

    def test_prepare_runs_exactly_when_images_do(self):
        self.assertEqual(condition('prepare'), condition('image'))
        self.assertEqual(condition('prepare'), condition('executor-image'))
        self.assertIn("needs.scope.outputs.backend == 'true'", condition('prepare'))
        self.assertIn("github.event_name != 'pull_request'", condition('prepare'))

    def test_images_start_after_prepare_not_after_the_regression(self):
        for name, output in (('image', 'applications'), ('executor-image', 'executors')):
            with self.subTest(job=name):
                self.assertEqual(needs(name), {'scope', 'prepare'})
                self.assertIn('fromJSON(needs.prepare.outputs.' + output, job(name))
                self.assertNotIn('needs.tests', job(name))

    def test_nothing_is_pushed_before_the_tests_job_has_succeeded(self):
        for name, publish in (('image', 'Publish only after image checks pass'),
                              ('executor-image', 'Publish only after executor checks pass')):
            with self.subTest(job=name):
                body = job(name)
                hold = 'Hold publication until the regression tests have passed'
                self.assertIn(hold, body)
                self.assertLess(body.index(hold), body.index(publish))
                self.assertIn('wait_for_job.py --job tests', step(name, hold))
                self.assertIn('GH_TOKEN: ${{ github.token }}', step(name, hold))
                # Unconditional: reused images wait too, so the hold is exercised on every run and a
                # publish step can never be reached without it.
                self.assertIsNone(condition_of_step(step(name, hold)))
                self.assertIsNotNone(condition_of_step(step(name, publish)))
                self.assertIn('actions: read', body.split('steps:')[0])

    def test_publish_steps_are_still_gated_on_the_offline_image_checks(self):
        body = job('image')
        self.assertLess(body.index('Check image boundary'), body.index('Hold publication'))
        self.assertLess(body.index('Hold publication'), body.index('docker push'))
        executor = job('executor-image')
        self.assertLess(executor.index('Check CPU execution image offline'), executor.index('Hold publication'))
        self.assertLess(executor.index('Hold publication'), executor.index('Publish only after executor checks pass'))

    def test_the_release_package_and_acceptance_still_need_the_tests(self):
        self.assertEqual(needs('package'), {'image', 'executor-image', 'tests'})
        self.assertLessEqual({'prepare', 'tests', 'image', 'executor-image', 'package'}, needs('acceptance'))
        self.assertLessEqual({'prepare', 'tests', 'image', 'executor-image', 'package'}, needs('summary'))

    def test_a_failed_or_missing_prepare_cannot_produce_a_deployable_acceptance(self):
        scope = pipeline.classify(['backend/app/main.py'], baseline='b' * 40, commit='a' * 40)
        results = {name: {'result': 'success'} for name in pipeline.required_results(scope)}
        for broken in ('image', 'package', 'tests'):
            with self.subTest(job=broken), self.assertRaises(ValueError):
                pipeline.aggregate(scope, {}, {**results, broken: {'result': 'skipped'}}, commit='a' * 40,
                                   run_id=1, attempt=1, identity=None)

    def test_old_runs_without_a_prepare_job_stay_valid_for_recovery(self):
        scope = pipeline.classify(['backend/app/main.py'], baseline='b' * 40, commit='a' * 40)
        self.assertNotIn('prepare', pipeline.required_jobs(scope))
        self.assertNotIn('prepare', pipeline.required_results(scope))

    def test_timing_collection_counts_the_new_job(self):
        self.assertIn('prepare', benchmark.CI_JOBS)


def condition_of_step(text):
    match = re.search(r'^        if: (.+)$', text, re.M)
    return match.group(1).strip() if match else None


def jobs_payload(states):
    return {'jobs': [{'name': name, 'status': status, 'conclusion': conclusion}
                     for name, status, conclusion in states]}


class WaitTests(unittest.TestCase):
    def run_wait(self, replies, **options):
        fetch = Mock(side_effect=replies)
        sleep = Mock()
        ticks = iter(range(0, 100000, 5))
        return fetch, sleep, lambda: wait_for_job.wait('o/r', '7', '2', 'tests', fetch=fetch, sleep=sleep,
                                                       clock=lambda: next(ticks), **options)

    def test_returns_when_the_job_already_succeeded(self):
        fetch, sleep, run = self.run_wait([jobs_payload([('tests', 'completed', 'success')])])
        self.assertEqual(run()['conclusion'], 'success')
        sleep.assert_not_called()
        self.assertIn('actions/runs/7/attempts/2/jobs', fetch.call_args.args[0])

    def test_waits_while_the_job_is_queued_or_running_then_passes(self):
        fetch, sleep, run = self.run_wait([jobs_payload([('tests', 'queued', None)]),
                                           jobs_payload([('tests', 'in_progress', None)]),
                                           jobs_payload([('tests', 'completed', 'success')])])
        run()
        self.assertEqual(sleep.call_count, 2)

    def test_every_non_success_ending_blocks_publication(self):
        for conclusion in ('failure', 'cancelled', 'skipped', 'timed_out', None):
            with self.subTest(conclusion=conclusion):
                fetch, sleep, run = self.run_wait([jobs_payload([('tests', 'completed', conclusion)])])
                with self.assertRaisesRegex(wait_for_job.JobNotSuccessful, 'nothing is published'):
                    run()

    def test_a_job_that_never_appears_or_never_finishes_times_out(self):
        for payload in (jobs_payload([('other', 'completed', 'success')]), jobs_payload([('tests', 'in_progress', None)])):
            with self.subTest(payload=payload):
                fetch, sleep, run = self.run_wait([payload] * 1000, timeout=60)
                with self.assertRaisesRegex(wait_for_job.JobNotSuccessful, 'did not finish'):
                    run()

    def test_ambiguous_job_names_never_count_as_success(self):
        fetch, sleep, run = self.run_wait([jobs_payload([('tests', 'completed', 'success'),
                                                         ('tests', 'completed', 'failure')])] * 1000, timeout=30)
        with self.assertRaises(wait_for_job.JobNotSuccessful):
            run()

    def test_a_brief_api_error_while_polling_does_not_stop_a_good_run_but_a_long_one_does(self):
        ok = jobs_payload([('tests', 'completed', 'success')])
        fetch, sleep, run = self.run_wait([RuntimeError('GitHub API check failed (HTTP 502)'), ok])
        self.assertEqual(run()['conclusion'], 'success')
        fetch, sleep, run = self.run_wait([RuntimeError('GitHub API check failed (HTTP 502)')] * 10)
        with self.assertRaisesRegex(wait_for_job.JobNotSuccessful, 'Could not read'):
            run()

    def test_reads_every_page_of_the_job_list(self):
        first = {'jobs': [{'name': 'x%d' % i, 'status': 'completed', 'conclusion': 'success'} for i in range(100)]}
        second = jobs_payload([('tests', 'completed', 'success')])
        fetch, sleep, run = self.run_wait([first, second])
        run()
        self.assertEqual(fetch.call_count, 2)
        self.assertIn('page=2', fetch.call_args.args[0])

    def test_the_command_reports_in_the_summary_and_fails_closed(self):
        env = {'GITHUB_REPOSITORY': 'o/r', 'GITHUB_RUN_ID': '7', 'GITHUB_RUN_ATTEMPT': '2', 'GH_TOKEN': 'x'}
        with patch.dict(os.environ, env, clear=True), patch('sys.argv', ['wait_for_job.py', '--job', 'tests']), \
                patch.object(wait_for_job, 'get', return_value=jobs_payload([('tests', 'completed', 'success')])), \
                redirect_stdout(io.StringIO()) as out:
            wait_for_job.main()
        self.assertTrue(json.loads(out.getvalue().strip().splitlines()[-1])['passed'])
        with patch.dict(os.environ, env, clear=True), patch('sys.argv', ['wait_for_job.py', '--job', 'tests']), \
                patch.object(wait_for_job, 'get', return_value=jobs_payload([('tests', 'completed', 'failure')])), \
                redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as raised:
            wait_for_job.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn('::error', out.getvalue())

    def test_reads_are_retried_through_the_shared_helper(self):
        from scripts.cicd import transient
        self.assertTrue(callable(transient.retry))
        with patch.object(wait_for_job.transient, 'retry', side_effect=lambda call: {'jobs': []}) as retry, \
                patch.dict(os.environ, {'GH_TOKEN': 'x'}):
            self.assertEqual(wait_for_job.get('repos/o/r/actions/runs/1/jobs'), {'jobs': []})
        retry.assert_called_once()


if __name__ == '__main__':
    unittest.main()
