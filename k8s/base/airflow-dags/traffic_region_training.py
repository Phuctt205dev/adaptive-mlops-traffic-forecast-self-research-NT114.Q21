from __future__ import annotations

import os
from datetime import datetime

import requests
from airflow import DAG
from airflow.operators.python import PythonOperator


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


def execute_training(**context):
    conf = context["dag_run"].conf or {}
    api_url = os.getenv("TRAFFIC_API_URL", "http://traffic-api:8000").rstrip("/")
    token = os.getenv("INTERNAL_TRAINING_TOKEN", "local-internal-training-token")
    response = requests.post(
        f"{api_url}/api/v1/internal/training-runs/{conf['training_run_id']}/execute",
        headers={"X-Internal-Token": token},
        timeout=None,
    )
    response.raise_for_status()
    return response.json()


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

    execute_region_training = PythonOperator(
        task_id="execute_region_training",
        python_callable=execute_training,
    )

    validate_training_conf >> execute_region_training
