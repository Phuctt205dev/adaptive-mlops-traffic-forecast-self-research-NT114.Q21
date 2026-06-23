from __future__ import annotations

import os
from datetime import datetime

import requests
from airflow.exceptions import AirflowSkipException
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.utils.trigger_rule import TriggerRule


DAG_ID = "traffic_region_training"


def validate_conf(**context):
    conf = context["dag_run"].conf or {}
    required = [
        "region_id",
        "dataset_id",
        "training_run_id",
        "train_start_date",
        "train_end_date",
    ]
    missing = [key for key in required if not conf.get(key)]
    if missing:
        raise ValueError(f"Missing DAG conf keys: {', '.join(missing)}")
    supported_models = {
        "random_forest_lag",
        "xgboost_lag",
        "lightgbm_lag",
        "lstm",
        "gru",
    }
    selected_models = conf.get("selected_models") or []
    if not selected_models:
        raise ValueError("selected_models must contain at least one model.")
    unsupported = sorted(set(selected_models) - supported_models)
    if unsupported:
        raise ValueError(f"Unsupported selected_models: {', '.join(unsupported)}")


def _post_internal_training(path):
    api_url = os.getenv("TRAFFIC_API_URL", "http://traffic-api:8000").rstrip("/")
    token = os.getenv("INTERNAL_TRAINING_TOKEN", "local-internal-training-token")
    response = requests.post(
        f"{api_url}/api/v1/internal{path}",
        headers={"X-Internal-Token": token},
        timeout=None,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise RuntimeError(
            f"Internal training API failed: {response.status_code} "
            f"{response.text}"
        ) from error
    return response.json()


def execute_tree_training(**context):
    conf = context["dag_run"].conf or {}
    return _post_internal_training(
        f"/training-runs/{conf['training_run_id']}/execute/tree"
    )


def execute_recurrent_training(model_name, **context):
    conf = context["dag_run"].conf or {}
    selected_models = {str(model).strip().lower() for model in conf.get("selected_models") or []}
    normalized_model = str(model_name).strip().lower()
    if normalized_model not in selected_models:
        raise AirflowSkipException(f"{model_name} was not selected for this training run.")
    return _post_internal_training(
        f"/training-runs/{conf['training_run_id']}/execute/recurrent/{model_name}"
    )


def finalize_training(**context):
    conf = context["dag_run"].conf or {}
    return _post_internal_training(
        f"/training-runs/{conf['training_run_id']}/finalize"
    )


with DAG(
    dag_id=DAG_ID,
    description="Orchestrate traffic model training for one region dataset.",
    start_date=datetime(2026, 1, 1),
    schedule_interval=None,
    catchup=False,
    max_active_runs=1,
    tags=["traffic", "mlops", "training"],
) as dag:
    validate_training_conf = PythonOperator(
        task_id="validate_training_conf",
        python_callable=validate_conf,
    )

    train_tree_models = PythonOperator(
        task_id="train_tree_models",
        python_callable=execute_tree_training,
    )

    train_lstm = PythonOperator(
        task_id="train_lstm",
        python_callable=execute_recurrent_training,
        op_kwargs={"model_name": "LSTM"},
    )

    train_gru = PythonOperator(
        task_id="train_gru",
        python_callable=execute_recurrent_training,
        op_kwargs={"model_name": "GRU"},
    )

    finalize_region_training = PythonOperator(
        task_id="finalize_region_training",
        python_callable=finalize_training,
        trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS,
    )

    validate_training_conf >> [train_tree_models, train_lstm, train_gru]
    [train_tree_models, train_lstm, train_gru] >> finalize_region_training
