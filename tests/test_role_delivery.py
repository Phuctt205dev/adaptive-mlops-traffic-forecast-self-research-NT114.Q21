"""Failure ordering and inverse ownership for role delivery, without a cluster."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.cicd import role_blueprints as bp
from scripts.cicd.roles_deploy import RolesDelivery, validate_delivery
from scripts.cicd.release import canonical,digest
from scripts.cicd import cd_state
from tests.test_cd_control import MemoryAPI,transaction


def pod_source(role='api'):
    main={'name':'traffic-api' if role=='api' else 'worker','image':'old', 'command':bp.OLD_RUNTIME,
          'ports':[{'containerPort':8000}], 'env':[{'name':'INTERNAL_TRAINING_TOKEN','valueFrom':{'secretKeyRef':{'name':'token','key':'token'}}}],
          'volumeMounts':[{'name':'cache','mountPath':'/app/models'}, {'name':'runtime','mountPath':'/opt/traffic-cloud'}],
          'resources':{'requests':{'memory':'1536Mi'}}}
    return {'spec':{'template':{'metadata':{'labels':{'app':'traffic-api','traffic-region-cleanup-peer':'true'}},
             'spec':{'containers':[main,{'name':'artifact-cache-sync','image':'old','command':['old'],
                       'envFrom':[{'secretRef':{'name':'minio-env'}}], 'volumeMounts':[{'name':'cache','mountPath':'/cache'}]}],
                      'volumes':[{'name':'cache','emptyDir':{}},{'name':'runtime','configMap':{'name':'old'}}],
                      'affinity':{'podAntiAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':[{'labelSelector':{'matchLabels':{'app':'traffic-api'}}}]}}}}}}


class Blueprints(unittest.TestCase):
    def test_api_has_ordered_native_sidecar_and_preserves_requests_and_secrets(self):
        value=bp.deployment(pod_source(),'api','api',{r:{'image':r} for r in ('api','tree','neural','sync')},'id')
        pod=value['spec']['template']['spec']; main=pod['containers'][0]
        self.assertEqual(value['spec']['replicas'],0)
        self.assertEqual(main['resources']['requests']['memory'],'1536Mi')
        self.assertIn('valueFrom',main['env'][0])
        self.assertEqual([c['name'] for c in pod['initContainers']],['restore-cache','artifact-sync'])
        self.assertEqual(pod['initContainers'][-1]['restartPolicy'],'Always')
        self.assertEqual(pod['terminationGracePeriodSeconds'],900)
        self.assertNotIn('traffic-region-cleanup-peer',value['spec']['template']['metadata']['labels'])
        self.assertEqual(pod['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution'][0]['labelSelector']['matchLabels']['app'],'api')
        self.assertEqual(len(pod['volumes']),1)

    def test_blueprint_refuses_literals_and_wrong_role_names(self):
        value=pod_source(); value['spec']['template']['spec']['containers'][0]['env'][0]={'name':'INTERNAL_TRAINING_TOKEN','value':'do-not-copy'}
        with self.assertRaisesRegex(ValueError,'literal credential'):
            bp.deployment(value,'api','api',{},'id')
        with self.assertRaises(ValueError):
            bp.deployment(pod_source(),'train-tree','neural',{},'id')


class DeliveryOrdering(unittest.TestCase):
    def operator(self,directory):
        op=object.__new__(RolesDelivery)
        op.path=Path(directory)/'receipt.json'; op.private_path=Path(directory)/'private'/'inverse.json'
        op.state={'commit':'a'*40,'patched':[],'guard_held':False}; op.inverses=[]
        return op

    def test_private_inverse_is_written_before_mutation_but_never_in_public_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            op=self.operator(directory)
            before={'replicas':2,'private_reference':'only-private-file'}
            op.get=Mock(return_value={'metadata':{'uid':'u','resourceVersion':'v'},'spec':before})
            def mutation(*args):
                self.assertTrue(op.private_path.exists())
                self.assertNotIn('only-private-file',op.path.read_text())
                return {}
            op.patch=Mock(side_effect=mutation)
            op.change('deployment','api',{'replicas':0})
            self.assertEqual(json.loads(op.private_path.read_text())[0]['before'],before)
            self.assertEqual(op.state['patched'],['api'])

    def test_rollback_does_not_overwrite_external_resource_change(self):
        with tempfile.TemporaryDirectory() as directory:
            op=self.operator(directory)
            op.inverses=[{'kind':'deployment','name':'api','uid':'u','before':{'replicas':0},'after':digest(canonical({'replicas':2}))}]
            op.get=Mock(return_value={'metadata':{'uid':'u'},'spec':{'replicas':3}})
            op.patch=Mock()
            with self.assertRaisesRegex(RuntimeError,'External resource drift'):
                op.rollback()
            op.patch.assert_not_called()

    def test_busy_training_defers_before_pause_or_cluster_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            op=self.operator(directory)
            op.plan=Mock(return_value={'idle':{'active':1,'running':0}})
            op.verify=Mock(side_effect=RuntimeError('Old version'))
            op.pause=Mock(); op.change=Mock()
            with patch.dict('os.environ',{'CI_RUN_ID':'123'}):
                op.apply()
            self.assertTrue(op.state['deferred']); self.assertFalse(op.state.get('started',False))
            self.assertEqual(op.state['retry_ci_run_id'],'123')
            op.pause.assert_not_called(); op.change.assert_not_called()

    def test_preview_failure_rolls_back_before_any_public_route_change(self):
        with tempfile.TemporaryDirectory() as directory:
            op=self.operator(directory)
            plan={'idle':{'active':0,'running':0,'paused':False,'drift_paused':False},'active':'traffic-api','target':'api','changes':{'api':{}},'targets':{'api':'api','tree':'train-tree','neural':'train-neural'}}
            op.plan=Mock(return_value=plan); op.verify=Mock(side_effect=RuntimeError('Old'))
            op.pause=Mock(); op.cluster=Mock(); op.cluster.remote.return_value={'active':0,'running':0}
            op.api=Mock(); op.start_guard=Mock(); op.change=Mock(); op.health=Mock(); op.endpoints=Mock()
            op.route=Mock(); op.business=Mock(side_effect=[{},RuntimeError('Preview failed')]); op.rollback=Mock()
            op.monitor_start=Mock(); op.monitor_stop=Mock()
            with self.assertRaisesRegex(RuntimeError,'Preview failed'):
                op.apply()
            self.assertEqual(op.route.call_args_list[0].args,('api-preview','api'))
            self.assertEqual(op.route.call_count,1)
            op.rollback.assert_called_once()

    def test_wrong_role_digest_binding_is_rejected(self):
        release={'commit':'a'*40,'release_id':'b'*64,'roles':{r:{'source_id':r} for r in ('api','tree','neural','sync')}}
        images={r:{'role':r,'source_id':r,'commit':'a'*40,'image':'ghcr.io/test/image@sha256:'+'c'*64} for r in release['roles']}
        value={'schema':'traffic-delivery/v2','commit':release['commit'],'release_id':release['release_id'],'images':images,'image':images['api']['image']}
        self.assertEqual(set(validate_delivery(release,value)),set(images))
        images['tree']['source_id']='wrong'
        with self.assertRaises(ValueError):
            validate_delivery(release,value)


class DeferredPolicy(unittest.TestCase):
    def test_safe_deferral_releases_ownership_without_success_or_failure_latch(self):
        state=cd_state.initial(); state['mode']='auto'
        api=MemoryAPI(state); store=cd_state.Store(api); item=transaction(mode='auto')
        cd_state.begin(store,item)
        receipt={'commit':item['commit'],'guard_held':False,'recovery_required':False,'preflight_only':True,
                 'started':False,'patched':[],'completed':False,'deferred':True,'retry_ci_run_id':'123'}
        with patch('scripts.cicd.cd_pending.save') as pending:
            result=cd_state.finish(store,item,'success',receipt,began=True)
        self.assertIn('deferred',result['outcome'])
        self.assertFalse(api.state['blocked']); self.assertIsNone(api.state['active']); self.assertIsNone(api.state['last_success'])
        self.assertEqual(pending.call_args.args[1]['ci_run_id'],'123')

    def test_deferred_flag_cannot_hide_a_partially_mutated_delivery(self):
        state=cd_state.initial(); state['mode']='auto'
        api=MemoryAPI(state); store=cd_state.Store(api); item=transaction(mode='auto')
        cd_state.begin(store,item)
        receipt={'commit':item['commit'],'guard_held':False,'recovery_required':False,'preflight_only':False,
                 'started':True,'patched':['api'],'completed':False,'deferred':True,'retry_ci_run_id':'123'}
        with patch('scripts.cicd.cd_pending.save') as pending:
            result=cd_state.finish(store,item,'success',receipt,began=True)
        self.assertEqual(result['outcome'],'failed'); self.assertTrue(api.state['blocked'])
        pending.assert_not_called()


class RestoreSafety(unittest.TestCase):
    def test_restore_rejects_parent_escape_before_fetch(self):
        from backend.app.services.restore_cache import restore
        client=Mock(); client.get_paginator.return_value.paginate.return_value=[{'Contents':[{'Key':'models/../outside','Size':1}]}]
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ',{'TRAFFIC_API_RUNTIME_BUCKET':'fixture'}), \
                patch('backend.app.services.region_cleanup_uploader.get_upload_client',return_value=client):
            with self.assertRaisesRegex(RuntimeError,'Unsafe artifact'):
                restore(Path(directory))
        client.get_object.assert_not_called()

    def test_size_mismatch_never_replaces_acknowledged_cache_file(self):
        import io
        from backend.app.services.restore_cache import restore
        client=Mock(); client.get_paginator.return_value.paginate.return_value=[{'Contents':[{'Key':'models/m','Size':2}]}]
        client.get_object.return_value={'Body':io.BytesIO(b'oversized')}
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ',{'TRAFFIC_API_RUNTIME_BUCKET':'fixture'}), \
                patch('backend.app.services.region_cleanup_uploader.get_upload_client',return_value=client):
            path=Path(directory)/'models/m'; path.parent.mkdir(); path.write_bytes(b'original')
            with self.assertRaisesRegex(RuntimeError,'exceeded its listed size'):
                restore(Path(directory))
            self.assertEqual(path.read_bytes(),b'original')
            self.assertFalse(path.with_name('m.restore.tmp').exists())


if __name__=='__main__':
    unittest.main()
