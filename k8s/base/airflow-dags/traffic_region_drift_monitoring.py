from __future__ import annotations

import os
from datetime import datetime

import requests
from airflow import DAG
from airflow.operators.python import PythonOperator


DAG_ID = "traffic_region_drift_monitoring"


def _parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def validate_conf(**context):
    conf = context["dag_run"].conf or {}
    max_regions = conf.get("max_regions")
    if max_regions is not None and int(max_regions) < 1:
        raise ValueError("max_regions must be greater than or equal to 1.")
    current_end = conf.get("current_end")
    if current_end:
        datetime.fromisoformat(str(current_end))


def check_feature_drift(**context):
    conf = context["dag_run"].conf or {}
    api_url = os.getenv("TRAFFIC_API_URL", "http://traffic-api:8000").rstrip("/")
    token = os.getenv("INTERNAL_TRAINING_TOKEN", "local-internal-training-token")
    auto_retrain = _parse_bool(conf.get("auto_retrain"), True)
    force_retrain = _parse_bool(conf.get("force_retrain"), False)
    max_regions = conf.get("max_regions")
    params = {"auto_retrain": str(auto_retrain).lower()}
    if force_retrain:
        params["force_retrain"] = "true"
    if max_regions is not None:
        params["max_regions"] = int(max_regions)
    if conf.get("current_end"):
        params["current_end"] = str(conf["current_end"])

    region_id = conf.get("region_id")
    if region_id:
        path = f"/api/v1/internal/drift/check/regions/{region_id}"
    else:
        path = "/api/v1/internal/drift/check"

    response = requests.post(
        f"{api_url}{path}",
        params=params,
        headers={"X-Internal-Token": token},
        timeout=None,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise RuntimeError(
            f"Internal drift API failed: {response.status_code} {response.text}"
        ) from error
    return response.json()


with DAG(
    dag_id=DAG_ID,
    description="Check feature drift and trigger retraining when drift is detected.",
    start_date=datetime(2026, 1, 1),
    schedule_interval="@daily",
    catchup=False,
    max_active_runs=1,
    tags=["traffic", "mlops", "drift"],
) as dag:
    validate_drift_conf = PythonOperator(
        task_id="validate_drift_conf",
        python_callable=validate_conf,
    )

    check_region_feature_drift = PythonOperator(
        task_id="check_region_feature_drift",
        python_callable=check_feature_drift,
    )

    validate_drift_conf >> check_region_feature_drift
