"""Same training graph, with polling for explicitly opted-in protocol2 runs.

Mount the reviewed legacy DAG outside Airflow's scanned DAG directory.
Automatic drift retraining keeps the legacy route until separately enabled.
"""
import importlib.util
from pathlib import Path
import sys
from airflow.exceptions import AirflowSkipException

control_spec = importlib.util.spec_from_file_location("traffic_airflow_control", "/opt/traffic-cloud/airflow_control.py")
control = importlib.util.module_from_spec(control_spec)
control_spec.loader.exec_module(control)

LEGACY = Path("/opt/traffic-cloud/legacy/traffic_region_training_worker.py")
spec = importlib.util.spec_from_file_location("traffic_training_legacy", LEGACY)
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


def complete_or_skip(response, conf, branch, context):
    if response.get("status") == "skipped":
        # PythonOperator will not push its return value after SkipException.
        # Persist the verified empty receipt first; finalize still needs it.
        legacy.receipt(response, conf, branch)
        context["ti"].xcom_push(key="return_value", value=response)
        raise AirflowSkipException(f"{branch} was not selected; no model was trained.")
    return response


def tree(**context):
    conf = legacy.configuration(context)
    response = control.train(conf, "tree") if conf.get("_execution_protocol") == 2 else legacy.execute_tree_training(**context)
    return complete_or_skip(response, conf, "tree", context)


def recurrent(model_name, **context):
    conf = legacy.configuration(context)
    response = control.train(conf, model_name.lower()) if conf.get("_execution_protocol") == 2 else legacy.execute_recurrent_training(model_name, **context)
    return complete_or_skip(response, conf, model_name.lower(), context)


def finalize(**context):
    conf = legacy.configuration(context)
    if conf.get("_execution_protocol") != 2:
        return legacy.finalize_training(**context)
    receipts = {branch: legacy.receipt(context["ti"].xcom_pull(task_ids=task), conf, branch)
                for branch, task in legacy.TASKS.items()}
    return control.finalize(conf, receipts)


def failed(context):
    conf = legacy.configuration(context)
    if conf.get("_execution_protocol") == 2:
        control.request_stop(conf)


dag = legacy.dag
# Airflow exposes description as read-only in the deployed version.
# Keep the existing DAG metadata while replacing only the task callables.
dag.get_task("train_tree_models").python_callable = tree
dag.get_task("train_lstm").python_callable = recurrent
dag.get_task("train_gru").python_callable = recurrent
dag.get_task("finalize_region_training").python_callable = finalize
dag.get_task("train_gru").trigger_rule = "none_failed"
dag.get_task("finalize_region_training").trigger_rule = "none_failed"
for task in dag.tasks:
    task.on_failure_callback = failed
