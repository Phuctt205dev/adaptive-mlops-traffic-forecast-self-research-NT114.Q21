"""Replacement traffic_region_training DAG; mount only in the routing stage.

Tree/finalize -> node02 worker. LSTM then GRU -> node03 worker.
Unselected recurrent models still publish explicit skipped-branch receipts.
No training request is made when this module is imported by the scheduler.
"""
from datetime import datetime
import os
import re
import uuid

import requests
from airflow import DAG
from airflow.operators.python import PythonOperator


DAG_ID = "traffic_region_training"
URLS = {"tree": "http://traffic-training-tree:8000", "recurrent": "http://traffic-training-worker:8000"}
NODES = {"tree": "mlops-node-02", "recurrent": "mlops-node-03"}
TASKS = {"tree": "train_tree_models", "lstm": "train_lstm", "gru": "train_gru"}


def configuration(context):
    conf = context["dag_run"].conf or {}
    for name in ("region_id", "dataset_id", "training_run_id"):
        if str(uuid.UUID(str(conf.get(name)))) != conf.get(name):
            raise ValueError("DAG requires canonical region/dataset/training UUIDs.")
    return conf


def validate_conf(**context):
    conf = configuration(context)
    if not conf.get("train_start_date") or not conf.get("train_end_date"):
        raise ValueError("DAG requires training start/end dates.")
    selected = conf.get("selected_models")
    supported = {"random_forest_lag", "xgboost_lag", "lightgbm_lag", "lstm", "gru"}
    if not isinstance(selected, list) or not selected or any(not isinstance(item, str) or item not in supported for item in selected):
        raise ValueError("selected_models must contain supported model names.")


def post_worker(role, path, payload=None):
    if role not in URLS:
        raise ValueError("Unknown training role.")
    token = os.environ.get("INTERNAL_TRAINING_TOKEN")
    if not token:
        raise RuntimeError("Internal training token is not configured.")
    # Dedicated URLs; never change the shared drift TRAFFIC_API_URL variable.
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(URLS[role] + "/api/v1/internal" + path,
            headers={"X-Internal-Token": token}, json=payload, timeout=(10, None), allow_redirects=False)
        try:
            if response.status_code != 200 or len(response.content) > 1024 * 1024:
                raise RuntimeError("Training worker HTTP request failed; response body omitted.")
            document = response.json()
        finally:
            response.close()
    identity = document.get("execution_worker", {}) if isinstance(document, dict) else {}
    prefixes = ("traffic-training-tree-", "train-tree-", "tree-next-") if role == "tree" else ("traffic-training-worker-", "train-neural-", "neural-next-")
    if (identity.get("role") != role or identity.get("node") != NODES[role]
            or identity.get("runtime_revision") != "branch-execution-v1"
            or not isinstance(identity.get("pod"), str) or not identity["pod"].startswith(prefixes)):
        raise RuntimeError("Training response did not identify the expected worker/node.")
    return document


def receipt(response, conf, branch):
    result = response.get("artifact_receipt") if isinstance(response, dict) else None
    keys = {"version", "region_id", "run_id", "branch", "bundle_id", "manifest_sha256"}
    if (not isinstance(result, dict) or set(result) != keys or type(result.get("version")) is not int
            or result["version"] != 1 or result["region_id"] != conf["region_id"]
            or result["run_id"] != conf["training_run_id"] or result["branch"] != branch
            or not isinstance(result["manifest_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", result["manifest_sha256"])):
        raise RuntimeError("Training branch receipt is missing or belongs to another branch/run.")
    if str(uuid.UUID(str(result["bundle_id"]))) != result["bundle_id"]:
        raise RuntimeError("Training branch bundle identity is invalid.")
    return result


def execute_tree_training(**context):
    conf = configuration(context)
    response = post_worker("tree", "/training-runs/" + conf["training_run_id"] + "/execute/tree")
    receipt(response, conf, "tree")
    return response


def execute_recurrent_training(model_name, **context):
    if model_name not in {"LSTM", "GRU"}:
        raise ValueError("Unsupported recurrent model.")
    conf = configuration(context)
    response = post_worker("recurrent", "/training-runs/" + conf["training_run_id"] + "/execute/recurrent/" + model_name)
    # Deliberately call the worker for an unselected model too. It publishes
    # a skipped manifest, performs no fit, and gives finalize a complete set.
    receipt(response, conf, model_name.lower())
    return response


def finalize_training(**context):
    conf = configuration(context)
    receipts = {branch: receipt(context["ti"].xcom_pull(task_ids=task_id), conf, branch)
                for branch, task_id in TASKS.items()}
    return post_worker("tree", "/training-runs/" + conf["training_run_id"] + "/finalize",
                       {"artifact_receipts": receipts})


with DAG(dag_id=DAG_ID, description="Train on dedicated node02/node03 workers with verified result receipts.",
         start_date=datetime(2026, 1, 1), schedule_interval=None, catchup=False,
         max_active_runs=1, max_active_tasks=2, tags=["traffic", "mlops", "training"]) as dag:
    validate_training_conf = PythonOperator(task_id="validate_training_conf", python_callable=validate_conf, retries=0)
    train_tree_models = PythonOperator(task_id="train_tree_models", python_callable=execute_tree_training, retries=0)
    train_lstm = PythonOperator(task_id="train_lstm", python_callable=execute_recurrent_training,
        op_kwargs={"model_name": "LSTM"}, retries=0)
    train_gru = PythonOperator(task_id="train_gru", python_callable=execute_recurrent_training,
        op_kwargs={"model_name": "GRU"}, retries=0)
    finalize_region_training = PythonOperator(task_id="finalize_region_training", python_callable=finalize_training, retries=0)
    validate_training_conf >> [train_tree_models, train_lstm]
    train_lstm >> train_gru
    [train_tree_models, train_gru] >> finalize_region_training
