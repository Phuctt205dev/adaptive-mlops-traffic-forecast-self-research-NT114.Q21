"""Controlled Kaggle graph shared by Dataset and manual/automatic Drift runs.

Tree, LSTM and GRU use separate attempts and notebooks. The two neural fits
may overlap on Kaggle; their receipts still join at one publication gate.
"""
import importlib.util
from pathlib import Path
import sys
from airflow.exceptions import AirflowSkipException
from airflow import DAG
from airflow.operators.python import PythonOperator

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


def cloud_configuration(context):
    conf = legacy.configuration(context)
    if conf.get("training_backend") != "kaggle" or conf.get("_execution_protocol") != 2:
        raise ValueError("This DAG accepts controlled Kaggle training only.")
    return conf


def validate_cloud(**context):
    legacy.validate_conf(**context)
    cloud_configuration(context)


def tree(**context):
    conf = cloud_configuration(context)
    response = control.train(conf, "tree")
    return complete_or_skip(response, conf, "tree", context)


def recurrent(model_name, **context):
    conf = cloud_configuration(context)
    response = control.train(conf, model_name.lower())
    return complete_or_skip(response, conf, model_name.lower(), context)


def finalize(**context):
    conf = cloud_configuration(context)
    receipts = {branch: legacy.receipt(context["ti"].xcom_pull(task_ids=task), conf, branch)
                for branch, task in legacy.TASKS.items()}
    return control.finalize(conf, receipts)


def failed(context):
    conf = legacy.configuration(context)
    if conf.get("_execution_protocol") == 2 and conf.get('training_backend') == 'kaggle':
        control.request_stop(conf)


with DAG(dag_id=legacy.DAG_ID, description="Controlled Kaggle branches with independent neural sessions.",
         start_date=legacy.dag.start_date, schedule_interval=None, catchup=False,
         max_active_runs=1, max_active_tasks=3, tags=["traffic", "mlops", "training"]) as dag:
    validate_training_conf = PythonOperator(task_id="validate_training_conf", python_callable=validate_cloud, retries=0)
    train_tree_models = PythonOperator(task_id="train_tree_models", python_callable=tree, retries=0)
    train_lstm = PythonOperator(task_id="train_lstm", python_callable=recurrent,
                                op_kwargs={"model_name": "LSTM"}, retries=0)
    train_gru = PythonOperator(task_id="train_gru", python_callable=recurrent,
                               op_kwargs={"model_name": "GRU"}, trigger_rule="none_failed", retries=0)
    finalize_region_training = PythonOperator(task_id="finalize_region_training", python_callable=finalize,
                                              trigger_rule="none_failed", retries=0)
    validate_training_conf >> [train_tree_models, train_lstm, train_gru]
    [train_tree_models, train_lstm, train_gru] >> finalize_region_training
for task in dag.tasks:
    task.on_failure_callback = failed
