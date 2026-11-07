"""Desired-state identity, runtime acknowledgements and failure boundaries."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from scripts.cicd.role_manifests import render,deployment_id,CONTRACT
from scripts.cicd.release import canonical,digest
from scripts.cicd.roles_deploy import RolesDelivery
from scripts.cicd.capacity import verify as capacity_verify
from backend.app.services import artifact_sync_control as sync

ROOT=Path(__file__).resolve().parents[1]


def images():
    return {role:{'source_id':digest(role.encode()),'image':'ghcr.io/owner/backend@sha256:'+digest(role.encode())}
            for role in ('api','tree','neural','sync')}


def operator(directory):
    op=object.__new__(RolesDelivery)
    op.path=Path(directory)/'receipt.json';op.private_path=Path(directory)/'private.json'
    op.state={'patched':[],'started':False,'guard_held':False};op.inverses=[];op.migration=None
    return op


class DesiredManifests(unittest.TestCase):
    def test_api_keeps_capacity_and_ordered_native_sidecar(self):
        value=render('api','api',images());pod=value['spec']['template']['spec']
        self.assertEqual(value['spec']['replicas'],2)
        self.assertEqual(pod['containers'][0]['resources']['requests']['memory'],'1536Mi')
        self.assertEqual(pod['terminationGracePeriodSeconds'],900)
        self.assertEqual([c['name'] for c in pod['initContainers']][-2:],['restore-cache','artifact-sync'])
        self.assertEqual(pod['initContainers'][-1]['restartPolicy'],'Always')
        self.assertEqual(pod['containers'][0]['envFrom'],pod['initContainers'][-1]['envFrom'])

    def test_pinned_legacy_fixture_source_has_not_changed(self):
        root=ROOT/'tests/fixtures/legacy_region_cleanup';value=json.loads((root/'manifest.json').read_bytes())
        for name,expected in value['files'].items():
            self.assertEqual(digest((root/name).read_bytes()),expected)

    def test_both_slots_have_same_identity_and_exact_runtime_role(self):
        for role,slots in {'api':('api','api-next'),'tree':('train-tree','tree-next'),'neural':('train-neural','neural-next')}.items():
            first,second=(render(role,slot,images()) for slot in slots)
            self.assertEqual(deployment_id(first),deployment_id(second))
            self.assertNotEqual(first['spec']['selector'],second['spec']['selector'])
            self.assertEqual(second['spec']['template']['spec']['containers'][0]['name'],'api' if role=='api' else 'worker')
            env={e['name']:e.get('value') for e in second['spec']['template']['spec']['containers'][0]['env']}
            self.assertEqual(env['REGION_CLEANUP_ROLE'],'api' if role=='api' else 'tree' if role=='tree' else 'recurrent')

    def test_manifest_change_is_detected_without_an_image_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);shutil.copytree(ROOT/'deployment/rke2/roles',root/'deploy')
            shutil.copy2(ROOT/'deployment/rke2/runtime-contract.json',root/'deploy/runtime-contract.json')
            before=render('api','api',images(),directory=root)
            path=root/'deploy/api.json';value=json.loads(path.read_text())
            value['spec']['template']['spec']['containers'][0]['readinessProbe']['timeoutSeconds']=7
            path.write_text(json.dumps(value))
            after=render('api','api',images(),directory=root)
            self.assertNotEqual(deployment_id(before),deployment_id(after))
            self.assertEqual(before['spec']['template']['spec']['containers'][0]['image'],after['spec']['template']['spec']['containers'][0]['image'])
            path.write_text(json.dumps(value,indent=5)+'\n')
            self.assertEqual(deployment_id(after),deployment_id(render('api','api',images(),directory=root)))

    def test_unbound_images_and_literal_credentials_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);shutil.copytree(ROOT/'deployment/rke2/roles',root/'deploy')
            shutil.copy2(ROOT/'deployment/rke2/runtime-contract.json',root/'deploy/runtime-contract.json')
            path=root/'deploy/api.json';value=json.loads(path.read_text())
            value['spec']['template']['spec']['containers'][0]['env'].append({'name':'DATABASE_URL','value':'private'})
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,'literal credential'):render('api','api',images(),directory=root)
        wrong=images();wrong['sync']['image']='busybox:latest'
        with self.assertRaisesRegex(ValueError,'immutable'):render('api','api',wrong)


class FlushAcknowledgements(unittest.TestCase):
    def module(self,acknowledge=True):
        class State:
            def __init__(self):self.acknowledged={}
        def upload(root,state):
            if acknowledge:state.acknowledged['models/probe']=((1,2,3,4,5),0)
            return 1
        return SimpleNamespace(UploadState=State,upload_cycle=Mock(side_effect=upload))

    def test_flush_ack_requires_all_stable_files(self):
        inventory={'models/probe':(1,2,3,4,5)}
        with patch.object(sync,'inventory',return_value=inventory):
            proof=sync.flush(module=self.module())
            self.assertTrue(proof['complete']);self.assertEqual(proof['acknowledged_files'],1)
            self.assertNotIn('models/probe',json.dumps(proof))
            with self.assertRaisesRegex(RuntimeError,'not acknowledged'):sync.flush(module=self.module(False))

    def test_a_file_changed_during_upload_never_confirms_retirement(self):
        with patch.object(sync,'inventory',side_effect=[{'models/probe':(1,2,3,4,5)},{'models/probe':(1,2,7,8,9)}]):
            with self.assertRaisesRegex(RuntimeError,'changed during flush'):sync.flush(module=self.module())

    def test_missing_cache_mount_is_not_a_successful_empty_flush(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'mount is unavailable'):
                sync.inventory(Path(directory)/'missing',self.module())

    def test_capabilities_do_not_load_uploader_or_touch_storage(self):
        out=io.StringIO()
        with redirect_stdout(out),patch.object(sync,'flush',side_effect=AssertionError('storage touched')):
            self.assertEqual(sync.main(['capabilities']),0)
        self.assertEqual(json.loads(out.getvalue())['schema'],'traffic-artifact-sync/v1')


class DeliveryFailureBoundaries(unittest.TestCase):
    def test_staging_capacity_refuses_memory_cpu_or_disk_shortfall(self):
        host={'cpus':4,'memory_available':4*1024**3,'disk_available':30*1024**3}
        assignment=[('node',{'containers':[{'resources':{'requests':{'cpu':'500m','memory':'1Gi'}}}]})]
        self.assertTrue(capacity_verify([],assignment,{'node':host})['scheduler_admission_still_required'])
        for key,value in [('cpus',0.5),('memory_available',1024**3),('disk_available',1024**3)]:
            with self.subTest(key=key),self.assertRaises(RuntimeError):
                capacity_verify([],assignment,{'node':{**host,key:value}})

    def test_status_only_version_change_can_retry_but_spec_drift_cannot(self):
        import copy
        for drift in (False,True):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as directory:
                op=operator(directory);op.cluster=Mock()
                current={'metadata':{'uid':'same','resourceVersion':'1','name':'api'},'spec':{'replicas':2}}
                latest=copy.deepcopy(current);latest['metadata']['resourceVersion']='2'
                if drift:latest['spec']['replicas']=1
                op.get=Mock(return_value=latest)
                op.cluster.call.side_effect=[RuntimeError('CAS failed'),{'updated':True}]
                if drift:
                    with self.assertRaises(RuntimeError):op.patch('deployment',current,[])
                    self.assertEqual(op.cluster.call.call_count,1)
                else:
                    self.assertTrue(op.patch('deployment',current,[])['updated'])
                    self.assertEqual(op.cluster.call.call_count,2)

    def test_noop_probe_failure_cannot_trigger_a_rollout(self):
        with tempfile.TemporaryDirectory() as directory:
            op=operator(directory)
            op.plan=Mock(return_value={'changes':{},'airflow_changed':False,'idle':{'active':0,'running':0}})
            op.verify=Mock(side_effect=RuntimeError('Observation unavailable'))
            op.pause=Mock();op.start_guard=Mock();op.change=Mock()
            with self.assertRaisesRegex(RuntimeError,'Observation unavailable'):op.apply()
            op.pause.assert_not_called();op.start_guard.assert_not_called();op.change.assert_not_called()
            self.assertFalse(op.state['started'])

    def test_unconfirmed_flush_stops_retirement_and_records_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            op=operator(directory);op.state['phase']='retire'
            with self.assertRaisesRegex(RuntimeError,'not fully acknowledged'):
                op.require_flush({'schema':'traffic-artifact-sync/v1','complete':False,'error_type':'UploadPaused'},'api')
            self.assertEqual(op.state['execution_failure']['phase'],'retire')

    def test_exec_error_preserves_safe_frames_without_exception_message(self):
        with tempfile.TemporaryDirectory() as directory:
            op=operator(directory);op.state['phase']='preflight';op.cluster=Mock()
            def execute(*args,**kwargs):
                out=io.StringIO()
                with redirect_stdout(out):exec(args[-1],{})
                return json.loads(out.getvalue())
            op.cluster.call.side_effect=execute
            with self.assertRaisesRegex(RuntimeError,'FileNotFoundError'):
                op.execute('api-probe','artifact-sync',"raise FileNotFoundError('PRIVATE-TOKEN-AND-PATH')")
            value=json.loads(op.path.read_text())
            self.assertEqual(value['execution_failure']['error_type'],'FileNotFoundError')
            self.assertNotIn('PRIVATE-TOKEN',json.dumps(value))


if __name__=='__main__':unittest.main()
