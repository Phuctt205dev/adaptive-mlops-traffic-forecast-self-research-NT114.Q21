from datetime import UTC, datetime

import requests

from backend.app.core.config import get_settings
from backend.app.core.errors import ApplicationError


def trigger_dag(dag_id: str, conf: dict, dag_run_id: str | None = None) -> dict:
    settings = get_settings()
    url = f"{settings.airflow_api_url}/dags/{dag_id}/dagRuns"
    payload = {
        "conf": conf,
        "logical_date": datetime.now(UTC).isoformat(),
    }
    if dag_run_id:
        payload["dag_run_id"] = dag_run_id

    try:
        response = requests.post(
            url,
            json=payload,
            auth=(settings.airflow_admin_username, settings.airflow_admin_password),
            timeout=10,
        )
    except requests.RequestException as error:
        raise ApplicationError(
            "airflow_unavailable",
            "Could not reach Airflow API.",
            503,
        ) from error

    if response.status_code >= 400:
        raise ApplicationError(
            "airflow_trigger_failed",
            f"Airflow rejected the DAG trigger: HTTP {response.status_code}.",
            502,
        )
    return response.json()
