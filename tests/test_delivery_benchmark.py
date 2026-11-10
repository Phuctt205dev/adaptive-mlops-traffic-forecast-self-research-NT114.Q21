"""Measurement contracts using fixtures; these are not performance samples."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.cicd import benchmark as b, pipeline, ci_scope, frontend_release

A='a'*40
RUN={'id':10,'run_attempt':2,'head_sha':A,'event':'push','status':'in_progress',
     'conclusion':None,'created_at':'2026-11-10T00:00:00Z'}
SCOPE=pipeline.classify(['frontend/src/main.jsx'],baseline='b'*40,commit=A,frontend=True)
CONTEXT={'experiment':'pilot1','workload':'front-v1','cache_state':'warm',
         'runner_profile':'ubuntu-24.04'}


def job(name, start, end, **kw):
    return {'id':name,'run_id':10,'run_attempt':2,'head_sha':A,'name':name,
            'status':'completed','conclusion':'success','started_at':f'2026-11-10T00:00:{start:02}Z',
            'completed_at':f'2026-11-10T00:00:{end:02}Z',**kw}


def measured():
    return b.measure({**RUN,'run_attempt':1},[job('scope',1,10),job('frontend-build',5,20),job('acceptance',20,22)],
                     scope=SCOPE,context=CONTEXT)


class MeasurementTests(unittest.TestCase):
    def test_pr_merge_is_bound_to_run_head_and_base_before_measurement(self):
        merge = 'c'*40; base = 'b'*40
        run = {**RUN, 'event': 'pull_request', 'pull_requests': [{'head': {'sha': A}, 'base': {'sha': base}}]}
        scope = pipeline.pr_scope(['README.md'], commit=merge, base=base)
        client = Mock()
        client.request.return_value = {'sha': merge, 'parents': [{'sha': A}, {'sha': base}]}
        tested = b.pr_tested_commit(client, run, scope)
        value = b.measure(run, [job('scope',1,3), job('acceptance',3,5)], scope=scope, tested_commit=tested)
        self.assertTrue(value['ci_accepted'])
        self.assertEqual(value['commit'], A)
        self.assertEqual(value['tested_commit'], merge)
        self.assertIsNone(value['seconds']['backend_commit_to_verify'])

    def test_unrelated_merge_cannot_be_used_for_pr_measurements(self):
        scope = pipeline.pr_scope(['README.md'], commit='c'*40, base='b'*40)
        run = {**RUN, 'event': 'pull_request', 'pull_requests': [{'head': {'sha': A}, 'base': {'sha': 'b'*40}}]}
        client = Mock()
        for parents in ([{'sha': A}, {'sha': 'd'*40}], [{'sha': A}], [{'sha': A}, {'sha': 'b'*40}, {'sha': 'd'*40}]):
            client.request.return_value = {'sha': 'c'*40, 'parents': parents}
            with self.subTest(parents=parents), self.assertRaises(ValueError):
                b.pr_tested_commit(client, run, scope)
        with self.assertRaises(ValueError):
            b.measure(RUN, [], tested_commit='c'*40)

    def test_docs_pr_measures_only_required_gate_without_fabricating_a_deploy(self):
        scope = pipeline.pr_scope(['README.md'], commit=A, base='b'*40)
        value = b.measure({**RUN, 'event': 'pull_request'},
                          [job('scope', 1, 3), job('acceptance', 3, 5)], scope=scope)
        self.assertTrue(value['ci_accepted'])
        self.assertFalse(value['coverage']['tests'])
        self.assertFalse(value['coverage']['frontend'])
        self.assertEqual(value['backend'], 'not_observed')
        self.assertIsNone(value['seconds']['backend_commit_to_verify'])
        with self.assertRaises(ValueError):
            b.measure(RUN, [], scope=scope)
        with self.assertRaises(ValueError):
            b.measure({**RUN, 'event': 'pull_request'}, [], scope=scope, receipt={})

    def test_frontend_pr_cannot_be_accepted_in_measurements_when_build_is_skipped(self):
        scope = pipeline.pr_scope(['frontend/src/main.jsx'], commit=A, base='b'*40)
        value = b.measure({**RUN, 'event': 'pull_request'},
                          [job('scope', 1, 3), job('acceptance', 3, 5),
                           job('frontend-build', 3, 4, conclusion='skipped')], scope=scope)
        self.assertFalse(value['ci_accepted'])

    def test_parallel_time_is_not_runner_sum_or_total_workflow(self):
        value=measured()
        self.assertEqual(value['seconds']['ci_wall'],22)
        self.assertEqual(value['seconds']['ci_execution_span'],21)
        self.assertEqual(value['seconds']['ci_runner'],26)
        self.assertFalse(value['workflow_complete'])
        self.assertTrue(value['timing_sample_eligible'])
        self.assertEqual(value['backend'],'not_observed')
        self.assertNotIn('workflow_total',value['seconds'])

    def test_unknown_negative_and_naive_timestamps_do_not_become_zero(self):
        for start,end in [(None,None),('bad','bad'),('2026-11-10T00:00:00','2026-11-10T00:00:01'),
                          ('2026-11-10T00:00:10Z','2026-11-10T00:00:01Z')]:
            self.assertIsNone(b.seconds(start,end))
        value=b.measure(RUN,[job('scope',1,5,completed_at=None)])
        self.assertIsNone(value['seconds']['ci_runner'])
        self.assertFalse(value['timing_sample_eligible'])

    def test_missing_or_failed_required_job_never_counts_as_fast_success(self):
        for conclusion in ('failure','cancelled','skipped'):
            value=b.measure(RUN,[job('scope',1,3),job('frontend-build',3,10,conclusion=conclusion),
                                job('acceptance',10,11)],scope=SCOPE)
            self.assertFalse(value['timing_sample_eligible'])
        cancelled={**RUN,'status':'completed','conclusion':'cancelled'}
        self.assertFalse(b.measure(cancelled,[job(n,1,10) for n in ('scope','frontend-build','acceptance')],scope=SCOPE)['timing_sample_eligible'])

    def test_exact_attempt_pagination_and_no_duplicate_jobs(self):
        client=Mock()
        client.request.side_effect=[{'total_count':2,'jobs':[job('scope',1,2)]},
                                    {'total_count':2,'jobs':[job('acceptance',2,3)]}]
        self.assertEqual(len(b.jobs_for(client,RUN)),2)
        self.assertIn('/attempts/2/jobs',client.request.call_args.args[1])
        for change in ({'run_attempt':1},{'head_sha':'b'*40},{'run_id':11}):
            client.request.side_effect=None
            client.request.return_value={'total_count':1,'jobs':[job('scope',1,2,**change)]}
            with self.subTest(change=change),self.assertRaises(ValueError):b.jobs_for(client,RUN)
        client.request.return_value={'total_count':2,'jobs':[job('scope',1,2)]*2}
        with self.assertRaisesRegex(ValueError,'Duplicate'):b.jobs_for(client,RUN)
        api_job=job('scope',1,2);api_job.pop('run_attempt')
        client.request.return_value={'total_count':1,'jobs':[api_job]}
        self.assertEqual(len(b.jobs_for(client,RUN)),1)

    def test_rerun_does_not_include_wait_since_original_run_creation(self):
        value=b.measure({**RUN,'created_at':'2026-11-01T00:00:00Z'},
                        [job('scope',1,2),job('frontend-build',2,10),job('acceptance',10,12)],scope=SCOPE)
        self.assertIsNone(value['seconds']['ci_wall'])
        self.assertIsNone(value['seconds']['initial_wait'])
        self.assertEqual(value['seconds']['ci_execution_span'],11)

    def test_noop_deferred_rollback_and_recovery_have_distinct_meanings(self):
        receipt={'schema':'traffic-role-delivery/v2','commit':A,'completed':True,
                 'guard_held':False,'recovery_required':False,'started':True,'patched':['api-green'],
                 'updated_at':'2026-11-10T00:00:30Z'}
        value=b.measure(RUN,[],receipt=receipt,commit_at='2026-11-10T00:00:00Z')
        self.assertEqual(value['backend'],'verified_deploy')
        self.assertEqual(value['seconds']['backend_commit_to_verify'],30)
        for changes,outcome in [({'up_to_date':True,'started':False,'patched':[]},'no_op'),
                                ({'deferred':True,'started':False,'patched':[],'preflight_only':True,'completed':False},'deferred'),
                                ({'recovery_required':True},'recovery_required'),
                                ({'rolled_back':True},'rolled_back')]:
            value=b.measure(RUN,[],receipt={**receipt,**changes})
            self.assertEqual(value['backend'],outcome)
            self.assertIsNone(value['seconds']['backend_commit_to_verify'])
            self.assertIsNone(value['seconds']['failed_release_to_verified_rollback'])
        value=b.measure(RUN,[],receipt={**receipt,'rolled_back':True,'failed_at':'2026-11-10T00:00:10Z',
                                       'recovered_at':'2026-11-10T00:00:20Z'})
        self.assertEqual(value['seconds']['failed_release_to_verified_rollback'],10)
        with self.assertRaises(ValueError):b.measure(RUN,[],receipt={**receipt,'commit':'b'*40})
        for inconsistent in ({'deferred':True},{'up_to_date':True}):
            value=b.measure(RUN,[],receipt={**receipt,**inconsistent})
            self.assertEqual(value['backend'],'unknown')
            self.assertIsNone(value['seconds']['backend_commit_to_verify'])

    def test_pages_failure_does_not_erase_verified_backend(self):
        jobs=[job('frontend-publication / publish',1,30,steps=[
            {'name':'Publish the verified artifact','conclusion':'failure','completed_at':RUN['created_at']}])]
        receipt={'schema':'traffic-role-delivery/v2','commit':A,'completed':True,'started':True,
                 'patched':['api-green'],'guard_held':False,'recovery_required':False}
        value=b.measure(RUN,jobs,receipt=receipt)
        self.assertFalse(value['pages_published']);self.assertEqual(value['backend'],'verified_deploy')
        jobs[0]['steps'][0].update(conclusion='success',completed_at='2026-11-10T00:00:30Z')
        self.assertTrue(b.measure(RUN,jobs)['pages_published'])

    def test_build_and_reuse_do_not_invent_cache_temperature(self):
        jobs=[job('image (api)',1,20,steps=[{'name':'Pull previously checked immutable image','conclusion':'success'}])]
        value=b.measure(RUN,jobs)
        self.assertEqual(value['images'],{'api':'reuse'})
        self.assertEqual(value['context']['cache_state'],'unknown')
        self.assertNotIn('token',json.dumps(value))
        with self.assertRaises(ValueError):b.measure(RUN,[],context={'workload':'untrusted\nannotation'})

    def test_legacy_baseline_keeps_its_actual_coverage(self):
        scope=ci_scope.classify(['backend/app/services/prediction.py'],baseline='b'*40,commit=A)
        jobs=[job(n,1,10) for n in b.ci_coverage.COMMON_JOBS]
        value=b.measure(RUN,jobs,scope=scope)
        self.assertTrue(value['ci_accepted']);self.assertTrue(value['coverage']['current'])
        self.assertEqual(value['context']['scenario'],'API-only')
        duplicate=[*jobs,job('acceptance',1,10)]
        self.assertFalse(b.measure(RUN,duplicate,scope=scope)['ci_accepted'])
        with self.assertRaises(ValueError):b.proof_scope({'schema':b.ci_coverage.SCHEMA},RUN)

    def test_collector_only_gets_metadata_and_writes_allowlisted_artifacts(self):
        client=Mock()
        jobs=[job(n,1,10) for n in ('scope','frontend-build','acceptance')]
        client.request.side_effect=[RUN,{'total_count':3,'jobs':jobs},
                                    {'committer':{'date':RUN['created_at']},'private':'never export'}]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'scope').mkdir()
            (root/'scope/ci-scope.json').write_text(json.dumps(SCOPE))
            args=['benchmark.py','collect','--run-id','10','--attempt','2','--inputs',str(root),'--output',str(root/'out')]
            with patch('sys.argv',args),patch.object(b,'GitHub',return_value=client), \
                 patch.dict('os.environ',{'GITHUB_REPOSITORY':'owner/repo'},clear=True):b.main()
            value=json.loads((root/'out/delivery-metrics.json').read_text())
            self.assertTrue(value['ci_accepted'])
            self.assertNotIn('private',json.dumps(value))
            self.assertTrue((root/'out/report.md').is_file())
        self.assertTrue(all(call.args[0]=='GET' for call in client.request.call_args_list))


class ComparisonTests(unittest.TestCase):
    def values(self,start,n,wall):
        values=[]
        for index in range(n):
            v=measured();v['run_id']=str(start+index);v['seconds']['ci_wall']=wall+index
            values.append(v)
        return values

    def test_pilot_median_min_max_and_coverage_change(self):
        before=self.values(1,5,30);after=self.values(10,5,20)
        for v in before:v['coverage']['current']=True
        row=next(r for r in b.compare(before,after)['rows'] if r['metric']=='ci_wall')
        self.assertEqual(row['baseline'],{'n':5,'median':32,'min':30,'max':34})
        self.assertEqual(row['candidate']['median'],22)
        self.assertEqual(row['status'],'pilot');self.assertTrue(row['coverage_changed'])
        self.assertEqual(row['reduction_percent'],31.25)

    def test_small_unknown_mismatched_and_failed_samples_do_not_claim_improvement(self):
        before=self.values(1,5,30);after=self.values(10,5,20)
        for change in ('small','unknown','cache','failed'):
            selected=copy.deepcopy(after)
            if change=='small':selected=selected[:4]
            elif change=='unknown':
                for v in selected:v['context']['workload']='unknown'
            elif change=='cache':
                for v in selected:v['context']['cache_state']='cold'
            else:
                for v in selected:v['timing_sample_eligible']=False
            result=b.compare(before,selected)
            self.assertTrue(all(r['reduction_percent'] is None for r in result['rows']))
        with self.assertRaisesRegex(ValueError,'Duplicate'):b.compare(before,before)
        fake={**after[0],'source':'fixture'}
        with self.assertRaises(ValueError):b.compare(before,[fake])


class FrontendSymlinkBoundaryTests(unittest.TestCase):
    def test_npm_symlinks_are_allowed_but_output_and_manifest_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'dist').mkdir();(root/'dist/index.html').write_text('checked')
            npm=root/'node_modules/.bin/vite';npm.parent.mkdir(parents=True);npm.touch()
            args={'commit':A,'tree':'front','config':{},'run_id':10,'attempt':2}
            # Simulate Linux npm links even on Windows without symlink privileges.
            with patch.object(Path,'is_symlink',lambda path:path==npm):
                frontend_release.build_manifest(root,**args)
                frontend_release.verify_build(root,**args)
            for link in (root/'dist',root/'dist/index.html',root/'frontend-build.json'):
                with self.subTest(link=link),patch.object(Path,'is_symlink',lambda path:path==link):
                    with self.assertRaisesRegex(ValueError,'symlinks'):frontend_release.build_manifest(root,**args)
                    with self.assertRaisesRegex(ValueError,'symlinks'):frontend_release.verify_build(root,**args)


if __name__=='__main__':unittest.main()
