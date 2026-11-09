"""Exercise the actual remote DAG probes without Airflow or a live cluster."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import copy
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import Mock, patch

from scripts.cicd.deploy import AIRFLOW_FILES, DAG_CHECK, DAG_COMPATIBILITY_CHECK
from scripts.cicd.release import digest
from scripts.cicd.roles_deploy import RolesDelivery


class AirflowDeliveryContractTests(unittest.TestCase):
    def dag(self, *, serial=False):
        graph = {'validate_training_conf': set(), 'train_tree_models': {'validate_training_conf'},
                 'train_lstm': {'validate_training_conf'}, 'train_gru': {'validate_training_conf'},
                 'finalize_region_training': {'train_tree_models', 'train_lstm', 'train_gru'}}
        if serial:
            graph.update(train_gru={'train_lstm'}, finalize_region_training={'train_tree_models', 'train_gru'})
        dag = SimpleNamespace(dag_id='traffic_region_training', max_active_runs=1,
                              max_active_tasks=2 if serial else 3, task_ids=list(graph))
        def function(name):
            scope = {}
            exec(compile('def ' + name + '(): pass', '/opt/airflow/dags/traffic_region_training.py', 'exec'), scope)
            return scope[name]
        names = {'validate_training_conf': 'validate_cloud', 'train_tree_models': 'tree',
                 'train_lstm': 'recurrent', 'train_gru': 'recurrent', 'finalize_region_training': 'finalize'}
        tasks = {name: SimpleNamespace(task_id=name, python_callable=function(call), on_failure_callback=function('failed'),
                   op_kwargs={'model_name': {'train_lstm': 'LSTM', 'train_gru': 'GRU'}.get(name)},
                   trigger_rule='none_failed' if name in {'train_gru', 'finalize_region_training'} else 'all_success')
                 for name, call in names.items()}
        dag.tasks = list(tasks.values())
        dag.get_task = lambda name: SimpleNamespace(**vars(tasks.get(name, SimpleNamespace())), upstream_task_ids=graph[name])
        return dag, graph

    def check(self, probe, dag, *, candidates=None, errors=None):
        output = io.StringIO()
        class DagBag:
            def __init__(self, **kwargs):
                self.dags = {}; self.import_errors = errors or {}
                for value in candidates if candidates is not None else [dag]:
                    self.bag_dag(value, value)
            def bag_dag(self, dag, root_dag):
                self.dags[dag.dag_id] = dag
        airflow, models = ModuleType('airflow'), ModuleType('airflow.models')
        models.DagBag = DagBag
        with patch.dict('sys.modules', {'airflow': airflow, 'airflow.models': models}), redirect_stdout(output):
            exec(probe, {})
        return json.loads(output.getvalue())['dag_valid']

    def test_duplicate_autoregistration_fails_even_if_selected_graph_is_correct(self):
        current, _ = self.dag()
        for candidates in ([current, copy.deepcopy(current)], [self.dag(serial=True)[0], current]):
            self.assertFalse(self.check(DAG_CHECK, current, candidates=candidates))
            self.assertTrue(self.check(DAG_COMPATIBILITY_CHECK, current, candidates=candidates))

    def test_wrong_callable_missing_callback_wrong_model_or_import_error_blocks_scheduler(self):
        for broken in ('callable', 'callback', 'kwargs', 'source', 'import'):
            with self.subTest(broken=broken):
                dag, _ = self.dag()
                task = dag.tasks[1]
                if broken == 'callable': task.python_callable = lambda: None
                elif broken == 'callback': task.on_failure_callback = None
                elif broken == 'kwargs': dag.tasks[2].op_kwargs['model_name'] = 'GRU'
                elif broken == 'source': task.python_callable.__code__ = task.python_callable.__code__.replace(co_filename='/unexpected.py')
                self.assertFalse(self.check(DAG_CHECK, dag, errors={'dag.py': 'Parse failed'} if broken == 'import' else None))

    def test_scheduler_requires_parallel_graph_and_webserver_accepts_both_reviewed_versions(self):
        current, _ = self.dag()
        previous, _ = self.dag(serial=True)
        self.assertTrue(self.check(DAG_CHECK, current))
        self.assertFalse(self.check(DAG_CHECK, previous))
        self.assertTrue(self.check(DAG_COMPATIBILITY_CHECK, current))
        self.assertTrue(self.check(DAG_COMPATIBILITY_CHECK, previous))

    def test_missing_receipt_edge_extra_task_or_wrong_model_dependency_fails_both_probes(self):
        for serial in (False, True):
            for broken in ('missing_final_receipt', 'wrong_start_dependency', 'extra_task'):
                with self.subTest(serial=serial, broken=broken):
                    dag, graph = self.dag(serial=serial)
                    if broken == 'missing_final_receipt':
                        graph['finalize_region_training'].remove('train_gru')
                    elif broken == 'wrong_start_dependency':
                        graph['train_lstm'] = {'train_tree_models'}
                    else:
                        graph['unreviewed_task'] = {'validate_training_conf'}
                        dag.task_ids.append('unreviewed_task')
                    self.assertFalse(self.check(DAG_CHECK, dag))
                    self.assertFalse(self.check(DAG_COMPATIBILITY_CHECK, dag))

    def test_wrong_dag_identity_or_concurrency_is_rejected(self):
        for serial in (False, True):
            for field, value in (('dag_id', 'unexpected'), ('max_active_runs', 2), ('max_active_tasks', 8)):
                with self.subTest(serial=serial, field=field):
                    dag, _ = self.dag(serial=serial)
                    setattr(dag, field, value)
                    self.assertFalse(self.check(DAG_CHECK, dag))
                    self.assertFalse(self.check(DAG_COMPATIBILITY_CHECK, dag))

    def test_rollout_checks_scheduler_strictly_and_keeps_previous_webserver_compatible(self):
        for serial_scheduler, invalid_webserver in ((False, False), (True, False), (False, True)):
            with self.subTest(serial_scheduler=serial_scheduler, invalid_webserver=invalid_webserver), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                hashes = {}
                for name in ('dag.py', 'airflow_control.py', 'legacy/traffic_region_training_worker.py'):
                    path = root / 'airflow' / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('synthetic source', encoding='utf-8')
                    hashes[name] = digest(path.read_bytes())
                scheduler, _ = self.dag(serial=serial_scheduler)
                webserver, graph = self.dag(serial=True)
                if invalid_webserver:
                    graph['finalize_region_training'].remove('train_gru')
                def deployment(name):
                    return {'metadata': {'name': name}, 'spec': {'template': {'spec': {
                        'containers': [{'name': name, 'image': 'reviewed-airflow', 'volumeMounts': [
                            {'name': 'cloud-code', 'mountPath': '/opt/traffic-cloud'}]}],
                        'volumes': [{'name': 'cloud-code', 'configMap': {'name': 'previous'}}]}}}}
                def remote(target, source):
                    name = target['metadata']['name']
                    if source == AIRFLOW_FILES:
                        return hashes
                    if source in (DAG_CHECK, DAG_COMPATIBILITY_CHECK):
                        self.assertEqual(source, DAG_CHECK if name == 'airflow-scheduler' else DAG_COMPATIBILITY_CHECK)
                        return {'dag_valid': self.check(source, scheduler if name == 'airflow-scheduler' else webserver)}
                    return {'executor': 'LocalExecutor'}
                cluster = SimpleNamespace(call=Mock(return_value={}), deployment=deployment, wait=Mock())
                op = SimpleNamespace(directory=root, release={'baseline': {'airflow_image': 'reviewed-airflow'}},
                                     cluster=cluster, remote=Mock(side_effect=remote), change=Mock())
                if serial_scheduler or invalid_webserver:
                    with self.assertRaises(RuntimeError):
                        RolesDelivery.airflow(op)
                else:
                    RolesDelivery.airflow(op)
                    self.assertEqual(op.change.call_args.args[1], 'airflow-scheduler')
                    self.assertEqual(op.change.call_count, 1)


if __name__ == '__main__':
    unittest.main()
