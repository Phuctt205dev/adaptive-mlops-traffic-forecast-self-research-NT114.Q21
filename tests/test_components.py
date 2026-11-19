"""Inventory/identity boundaries and workload safety, without Docker or a cluster."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.cicd import components, pipeline, roles_release
from scripts.cicd.bind_roles import bind
from scripts.cicd.role_manifests import render
from scripts.cicd.roles_deploy import RolesDelivery, validate_delivery
from src.local_training.kubernetes import Kubernetes

ROOT = Path(__file__).resolve().parents[1]
A = 'a' * 40
B = 'b' * 40


class ComponentDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.package = Path(cls.temp.name) / 'release'
        # Exercise both profiles independently of the production enable flag.
        source_read = components.read
        def disabled_fixture(root, path):
            value = source_read(root, path)
            if path == 'deployment/cicd/local-training.json':
                config = json.loads(value)
                return roles_release.canonical({**config, 'enabled': False})
            return value
        with patch.object(components, 'read', side_effect=disabled_fixture):
            cls.release = roles_release.build(ROOT, cls.package, A)
        cls.bindings = {name: {'role':name, 'source_id':spec['source_id'], 'commit':A,
                              'image':'ghcr.io/test/backend@sha256:'+hashlib.sha256(name.encode()).hexdigest()}
                        for name, spec in {**cls.release['roles'], **cls.release['executors']}.items()}
        directory = Path(cls.temp.name)/'bindings'; directory.mkdir()
        for name, value in cls.bindings.items():
            (directory/('image-'+name+'.json')).write_text(json.dumps(value))
        cls.delivery = bind(cls.package, directory, A)

    def executor_id(self, changed=None):
        original = Path.read_bytes
        def read(path):
            value = original(path)
            return value + b'\n# simulated source edit\n' if str(path).replace('\\','/').endswith('/'+str(changed)) else value
        with patch.object(Path, 'read_bytes', read):
            spec=components.load()['components']['local-training']
            return components.prepare_executor(ROOT,'local-training',spec,roles_release.closure,A)[1]['source_id']

    def test_inventory_rejects_missing_core_unknown_profiles_paths_and_keys(self):
        original = components.load()
        bad=[]
        value=copy.deepcopy(original);del value['components']['api'];bad.append(value)
        value=copy.deepcopy(original);value['components']['local-training']['dockerfile']='../Dockerfile';bad.append(value)
        value=copy.deepcopy(original);value['components']['local-training']['kind']='database';bad.append(value)
        value=copy.deepcopy(original);value['components']['local-training']['privileged']=True;bad.append(value)
        value=copy.deepcopy(original);value['components']['local-training']['dockerfile']='untracked-layout/Dockerfile';bad.append(value)
        for value in bad:
            with self.subTest(value=value),self.assertRaises(ValueError):components.validate(value)

    def test_executor_fingerprint_ignores_ui_api_and_unrelated_ml_changes(self):
        expected=self.executor_id()
        for path in ('frontend/src/main.jsx','backend/app/services/serving_runtime.py','src/time_series_training.py'):
            with self.subTest(path=path):self.assertEqual(expected,self.executor_id(path))

    def test_executor_tracks_recipe_nested_dependencies_entrypoint_and_check(self):
        expected=self.executor_id()
        for path in ('infra/local-training/Dockerfile','requirements/roles/constraints.txt',
                     'requirements/roles/tree.txt','src/local_training/job.py','scripts/cicd/executor_checks.py'):
            with self.subTest(path=path):self.assertNotEqual(expected,self.executor_id(path))

    def test_packaged_executor_entrypoint_imports_without_checkout_fallback(self):
        context=self.package/'executors/local-training'
        code="import sys;from pathlib import Path;sys.path.insert(0,str(Path.cwd()));import src.local_training.job as job;assert Path(job.__file__).resolve().is_relative_to(Path.cwd())"
        result=subprocess.run([sys.executable,'-I','-B','-c',code],cwd=context,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_requirements_reject_escape_and_cycles(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'a.txt').write_text('-r b.txt\n');(root/'b.txt').write_text('-c a.txt\n')
            with self.assertRaisesRegex(ValueError,'Cyclic'):components.requirements(root,['a.txt'])
            (root/'b.txt').write_text('-r ../outside.txt\n')
            with self.assertRaisesRegex(ValueError,'Unsafe'):components.requirements(root,['a.txt'])

    def test_resource_parent_symlink_cannot_escape_the_checked_source(self):
        with patch.object(Path,'is_symlink',lambda path: path.name=='cloud_training'):
            with self.assertRaisesRegex(ValueError,'resources cannot follow'):
                roles_release.closure(ROOT,'local-training',entrypoints=['backend.app.core.application_error'],
                                      resources=['src/cloud_training/notebook.py'],allow_training=True)

    def test_new_executor_gets_required_job_without_workflow_name_changes(self):
        registry=components.load()
        registry['components']['evaluation']=copy.deepcopy(registry['components']['local-training'])
        scope=pipeline.classify(['infra/evaluation/Dockerfile'],baseline=B,commit=A,registry=registry)
        self.assertIn('executor-image (evaluation)',pipeline.required_jobs(scope))
        self.assertFalse(scope['full']);self.assertFalse(scope['current'])
        # Read the exact accepted inventory, not today's default inventory.
        with patch.object(components,'load',side_effect=AssertionError('Do not reinterpret historical inventory')):
            pipeline.validate_scope(scope,A)

    def test_frontend_scope_never_requires_executor_or_backend(self):
        scope=pipeline.classify(['frontend/src/main.jsx'],baseline=B,commit=A,frontend=True)
        self.assertFalse(scope['backend'])
        self.assertFalse(any('image' in name for name in pipeline.required_jobs(scope)))

    def test_local_dockerfile_alone_is_backend_fast_input_and_source_freshness_input(self):
        scope=pipeline.classify(['infra/local-training/Dockerfile'],baseline=B,commit=A)
        self.assertTrue(scope['backend']);self.assertTrue(scope['tests'])
        self.assertFalse(scope['full']);self.assertFalse(scope['current']);self.assertFalse(scope['frontend'])
        workflow=(ROOT/'.github/workflows/backend-ci.yml').read_text()
        push=workflow.split('  push:',1)[1].split('  workflow_dispatch:',1)[0]
        self.assertNotIn('paths:',push)

    def test_missing_wrong_or_extra_executor_binding_is_rejected(self):
        for change in ('missing','source','extra','registry','schema'):
            value=copy.deepcopy(self.delivery)
            if change=='missing':value['executors']={}
            if change=='source':value['executors']['local-training']['source_id']='0'*64
            if change=='extra':value['executors']['unknown']=value['executors']['local-training']
            if change=='registry':value['component_registry_sha256']='0'*64
            if change=='schema':value['schema']='traffic-delivery/v2'
            with self.subTest(change=change),self.assertRaises(ValueError):validate_delivery(self.release,value)

    def test_acceptance_cannot_downgrade_or_skip_executor(self):
        scope=pipeline.classify(['backend/app/core/config.py'],baseline=B,commit=A)
        results={name:{'result':'success'} for name in pipeline.required_results(scope)}
        pipeline.aggregate(scope,{},results,commit=A,run_id=1,attempt=1,identity=self.delivery)
        for status in ('skipped','failure','cancelled'):
            bad={**results,'executor-image':{'result':status}}
            with self.subTest(status=status),self.assertRaises(ValueError):
                pipeline.aggregate(scope,{},bad,commit=A,run_id=1,attempt=1,identity=self.delivery)
        missing=copy.deepcopy(self.delivery);missing['executors']={}
        with self.assertRaises(ValueError):pipeline.aggregate(scope,{},results,commit=A,run_id=1,attempt=1,identity=missing)

    def test_disabled_local_does_not_mount_dispatcher_token_and_config_is_digest_bound(self):
        images=self.delivery['images'];executors=self.delivery['executors']
        for role in ('api','tree','neural'):
            value=render(role,{'api':'api','tree':'train-tree','neural':'train-neural'}[role],images,
                         directory=self.package,executors=executors)
            pod=value['spec']['template']['spec']
            self.assertIs(pod['automountServiceAccountToken'],False)
            main=next(c for c in pod['containers'] if c['name']!='artifact-sync')
            env={v['name']:v.get('value') for v in main['env']}
            self.assertEqual(env['LOCAL_TRAINING_IMAGE'],executors['local-training']['image'])
            self.assertEqual(env['LOCAL_TRAINING_ENABLED'],'false')

    def test_enabled_profile_only_gives_tokens_to_workers_and_changes_deployment_identity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);shutil.copytree(self.package/'deploy',root/'deploy')
            config=json.loads((root/'deploy/local-training.json').read_bytes());config['enabled']=True
            (root/'deploy/local-training.json').write_text(json.dumps(config))
            for role,slot in [('api','api'),('tree','train-tree'),('neural','train-neural')]:
                value=render(role,slot,self.delivery['images'],directory=root,executors=self.delivery['executors'])
                pod=value['spec']['template']['spec']
                self.assertEqual(pod['automountServiceAccountToken'],role!='api')
                if role!='api':self.assertEqual(pod['serviceAccountName'],'traffic-training-dispatcher')
                before=render(role,slot,self.delivery['images'],directory=self.package,executors=self.delivery['executors'])
                self.assertNotEqual(value['spec']['template']['metadata']['annotations'],before['spec']['template']['metadata']['annotations'])

    def test_execution_artifact_tampering_fails_source_verification(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'release';shutil.copytree(self.package,root,ignore=shutil.ignore_patterns('delivery.json'))
            (root/'executors/local-training/src/local_training/job.py').write_text('tampered')
            with self.assertRaisesRegex(ValueError,'source differs'):roles_release.verify(root)

    def test_v2_release_and_v3_scope_remain_readable_without_executor(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'release';shutil.copytree(self.package,root,ignore=shutil.ignore_patterns('delivery.json'))
            release=copy.deepcopy(self.release);release['schema']=roles_release.LEGACY_SCHEMA
            del release['executors'];del release['component_registry_sha256']
            extra={p for p in release['files'] if p=='components.json' or p.startswith('executors/') or p in
                   {'deploy/local-training.json','deploy/local-training-rbac.yaml'}}
            for path in extra:
                (root/path).unlink();del release['files'][path]
            release['release_id']=components.digest(components.canonical({k:v for k,v in release.items() if k!='release_id'}))
            (root/'release.json').write_text(json.dumps(release))
            roles_release.verify(root,expected_commit=A)
            identity=copy.deepcopy(self.delivery);identity['schema']='traffic-delivery/v2'
            del identity['executors'];del identity['component_registry_sha256']
            scope=pipeline.classify_v3(['backend/app/core/config.py'],baseline=B,commit=A)
            self.assertNotIn('executor-image',pipeline.required_results(scope))
            pipeline.aggregate(scope,{}, {n:{'result':'success'} for n in pipeline.required_results(scope)},
                               commit=A,run_id=1,attempt=1,identity=identity)

    def test_pending_or_terminating_local_work_is_fenced_even_without_database_execution(self):
        op=object.__new__(RolesDelivery);op.executors=self.delivery['executors'];op.cluster=Mock()
        op.cluster.call.side_effect=[{'items':[{'metadata':{'name':'local-train-1'},'status':{}}]},
                                    {'items':[{'metadata':{'name':'pod-1'},'status':{'phase':'Pending'}}]}]
        self.assertEqual(op.local_work(),['job/local-train-1','pod/pod-1'])
        op.cluster.call.side_effect=[{'items':[{'metadata':{'name':'finished'},'status':{'conditions':[{'type':'Complete','status':'True'}]}}]},
                                    {'items':[{'metadata':{'name':'pod-2','deletionTimestamp':'now'},'status':{'phase':'Succeeded'}}]}]
        self.assertEqual(op.local_work(),['pod/pod-2'])

    def test_dispatcher_readiness_requires_create_delete_and_log_permission(self):
        kube=object.__new__(Kubernetes);kube.namespace='traffic-mlops'
        def call(method,path,**kw):
            if method=='POST':return {'status':{'allowed':kw['document']['spec']['resourceAttributes']['verb']!='delete'}}
            return {'items':[]}
        kube.call=Mock(side_effect=call)
        with self.assertRaisesRegex(RuntimeError,'permissions'):kube.probe()
        kube.call=Mock(return_value={'status':{'allowed':True},'items':[]})
        self.assertTrue(kube.probe())
        self.assertEqual(sum(c.args[0]=='POST' for c in kube.call.call_args_list),3)
        access=kube.call.call_args_list[-1].kwargs['document']['spec']['resourceAttributes']
        self.assertEqual((access['resource'],access['subresource']),('pods','log'))

    def test_executor_reuse_is_measured_and_missing_executor_prevents_ci_acceptance(self):
        from scripts.cicd import benchmark
        scope=pipeline.classify(['backend/app/core/config.py'],baseline=B,commit=A)
        jobs=[{'name':name,'status':'completed','conclusion':'success',
               'started_at':'2026-11-18T00:00:01Z','completed_at':'2026-11-18T00:00:02Z',
               'steps':[{'name':'Confirm the immutable executor digest','conclusion':'success'}]}
              for name in pipeline.required_jobs(scope)]
        run={'id':1,'run_attempt':1,'head_sha':A,'event':'push','status':'completed','conclusion':'success',
             'created_at':'2026-11-18T00:00:00Z'}
        report=benchmark.measure(run,jobs,scope=scope)
        self.assertTrue(report['ci_accepted']);self.assertEqual(report['images']['local-training'],'reuse')
        report=benchmark.measure(run,[j for j in jobs if not j['name'].startswith('executor-image')],scope=scope)
        self.assertFalse(report['ci_accepted'])

    def test_local_verify_requires_api_capability_after_both_worker_digests_match(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'deploy').mkdir()
            config=json.loads((self.package/'deploy/local-training.json').read_bytes());config['enabled']=True
            (root/'deploy/local-training.json').write_text(json.dumps(config))
            op=object.__new__(RolesDelivery);op.directory=root;op.executors=self.delivery['executors'];op.cluster=Mock()
            worker={'local_training':True,'image':op.executors['local-training']['image']}
            op.remote=Mock(side_effect=[worker,worker,{'local_training':False,'supported_backends':['kaggle']}])
            with self.assertRaisesRegex(RuntimeError,'API did not expose'):op.verify_local('api',('tree','neural'))
            op.remote=Mock(side_effect=[worker,worker,{'local_training':True,'supported_backends':['kaggle','local']}])
            op.verify_local('api',('tree','neural'))
            self.assertEqual(op.remote.call_count,3)


if __name__ == '__main__':
    unittest.main()
