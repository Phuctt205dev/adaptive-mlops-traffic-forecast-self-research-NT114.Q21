"""Lost exec responses must be confirmed without replaying deployment writes."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.cicd.deploy import Cluster, ClusterOperationError
from scripts.cicd.roles_deploy import RolesDelivery


def failure(category='unknown'):
    return ClusterOperationError(('exec','api-pod','-c','api','--','python','private-code'),category,1)


def job(*,uid='original',condition=None):
    return {'metadata':{'uid':uid},'status':{'conditions':
        [] if condition is None else [{'type':condition,'status':'True'}]}}


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.op=object.__new__(RolesDelivery)
        self.op.path=Path(self.directory.name)/'receipt.json'
        self.op.state={'phase':'release_admission','guard_held':True,'guard_pod':'guard-pod','guard_job':'guard-job'}
        self.op.api=Mock(return_value={})
        self.op.cluster=Mock()
        self.op.remote=Mock()

    def test_lost_pause_ack_is_confirmed_for_each_dag_without_repeating_patch(self):
        for drift in (False,True):
            for desired in (False,True):
                with self.subTest(drift=drift,desired=desired):
                    self.op.remote.reset_mock()
                    self.op.remote.side_effect=[failure(),{'paused':desired}]
                    self.assertIs(self.op.pause(desired,drift=drift),desired)
                    calls=self.op.remote.call_args_list
                    self.assertEqual(len(calls),2)
                    self.assertIn('c.patch(',calls[0].args[1])
                    self.assertIn('c.get(',calls[1].args[1])
                    self.assertNotIn('c.patch(',calls[1].args[1])
                    expected='drift_dag_id' if drift else 'training_dag_id'
                    self.assertIn(expected,calls[1].args[1])
                    self.assertEqual(self.op.state['confirmed_operations'][-1]['confirmed_by'],'dag_readback')

    def test_wrong_or_unavailable_readback_preserves_original_failure(self):
        for readback in ({'paused':True},{'paused':'False'},failure('timeout')):
            with self.subTest(readback=type(readback).__name__):
                original=failure()
                self.op.remote.side_effect=[original,readback]
                with self.assertRaises(ClusterOperationError) as caught:
                    self.op.pause(False)
                self.assertIs(caught.exception,original)
                self.assertNotIn('confirmed_operations',self.op.state)

    def test_read_failures_and_permanent_write_errors_are_not_hidden(self):
        for desired,category in ((None,'timeout'),(False,'forbidden'),(False,'unauthorized'),
                                  (False,'tls_verification'),(False,'not_found')):
            with self.subTest(desired=desired,category=category):
                self.op.remote.reset_mock()
                self.op.remote.side_effect=failure(category)
                with self.assertRaises(ClusterOperationError):self.op.pause(desired)
                self.assertEqual(self.op.remote.call_count,1)

    def prepare_release(self,conditions,*,error=None):
        self.op.guard_status=Mock(return_value={'state':'held'})
        self.op.flag=Mock(side_effect=error)
        self.op.get=Mock(side_effect=[job(),*conditions])

    def test_release_ack_loss_requires_same_job_completion_and_no_second_write(self):
        self.prepare_release([job(),job(condition='Complete')],error=failure())
        with patch('scripts.cicd.roles_deploy.time.sleep'):
            self.op.release_guard()
        self.op.flag.assert_called_once_with('release',True)
        self.assertFalse(self.op.state['guard_held'])
        self.assertEqual(self.op.state['confirmed_operations'][-1]['confirmed_by'],'same_job_complete')

    def test_normal_release_still_requires_job_completion(self):
        self.prepare_release([job(condition='Complete')])
        self.op.release_guard()
        self.assertFalse(self.op.state['guard_held'])
        self.assertNotIn('confirmed_operations',self.op.state)
        self.op.flag.assert_called_once_with('release',True)

    def test_incomplete_release_preserves_error_and_guard_for_recovery(self):
        original=failure()
        self.prepare_release([job()],error=original)
        with patch('scripts.cicd.roles_deploy.time.monotonic',side_effect=[0,0,21]), \
                patch('scripts.cicd.roles_deploy.time.sleep'):
            with self.assertRaises(ClusterOperationError) as caught:self.op.release_guard()
        self.assertIs(caught.exception,original)
        self.op.flag.assert_called_once_with('release',True)
        self.assertTrue(self.op.state['guard_held'])

    def test_failed_or_replaced_job_is_never_accepted_as_released(self):
        for terminal in (job(condition='Failed'),job(uid='replacement',condition='Complete')):
            with self.subTest(terminal=terminal):
                self.prepare_release([terminal],error=failure())
                with self.assertRaises(RuntimeError):self.op.release_guard()
                self.assertTrue(self.op.state['guard_held'])

    def test_lost_lease_and_forbidden_release_remain_failures(self):
        self.prepare_release([job(condition='Complete')])
        self.op.guard_status.return_value={'state':'released'}
        with self.assertRaises(RuntimeError):self.op.release_guard()
        self.op.flag.assert_not_called()
        self.prepare_release([job(condition='Complete')],error=failure('forbidden'))
        with self.assertRaises(ClusterOperationError):self.op.release_guard()
        self.assertTrue(self.op.state['guard_held'])
        self.assertEqual(self.op.get.call_count,1)

    def test_exec_context_survives_rollback_without_code_or_stderr(self):
        original=failure('remote_exit')
        self.op.cluster.call.side_effect=original
        with self.assertRaises(ClusterOperationError):
            self.op.cluster_exec('api-pod','api','private-token',purpose='set_training_dag_pause')
        self.assertEqual(original.facts['purpose'],'set_training_dag_pause')
        self.assertEqual(original.facts['phase'],'release_admission')
        self.assertEqual(original.facts['name'],'api-pod')
        self.assertNotIn('private',json.dumps(original.facts))


class ExecFailureFacts(unittest.TestCase):
    def test_target_and_remote_exit_are_reported_without_replaying_exec(self):
        cluster=object.__new__(Cluster);cluster.prefix=['kubectl']
        result=subprocess.CompletedProcess([],1,'','private-token command terminated with exit code 137')
        with patch('scripts.cicd.deploy.subprocess.run',return_value=result) as run:
            with self.assertRaises(ClusterOperationError) as caught:
                cluster.call('exec','api-pod','-c','api','--','python','-c','private-code')
        self.assertEqual(run.call_count,1)
        self.assertEqual(caught.exception.facts,{'operation':'exec','resource':'pod','name':'api-pod',
            'container':'api','category':'remote_exit','attempts':1,'exit_code':1,'remote_exit_code':137})
        self.assertNotIn('private',str(caught.exception))

    def test_command_arguments_after_separator_are_never_used_as_target_metadata(self):
        error=ClusterOperationError(('exec','pod','--','python','-c','private-token'), 'unknown',1)
        self.assertNotIn('container',error.facts)
        self.assertNotIn('private',str(error))
        error=ClusterOperationError(('exec','pod','--container=guard','--','python'), 'unknown',1)
        self.assertEqual(error.facts['container'],'guard')


if __name__=='__main__':unittest.main()
