from datetime import UTC, datetime
import json
import time
from urllib.parse import quote

import requests

from backend.app.core.config import get_settings
from backend.app.core.errors import ApplicationError


def get_dag_run(dag_id: str, dag_run_id: str) -> dict:
    """Bounded read for dispatch reconciliation; never triggers/resumes a DAG."""
    settings = get_settings()
    url = settings.airflow_api_url.rstrip('/') + '/dags/' + quote(dag_id, safe='') + '/dagRuns/' + quote(dag_run_id, safe='')
    deadline = time.monotonic() + 6
    with requests.Session() as session:
        session.trust_env = False
        with session.get(url, auth=(settings.airflow_admin_username, settings.airflow_admin_password),
                         timeout=(2, 3), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ApplicationError('airflow_status_unavailable', 'Airflow status could not be confirmed.', 503)
            chunks, size = [], 0
            for chunk in response.iter_content(16384):
                if time.monotonic() > deadline:
                    raise TimeoutError('Airflow status deadline exceeded.')
                size += len(chunk)
                if size > 128 * 1024:
                    raise ValueError('Airflow status exceeds its budget.')
                chunks.append(chunk)
    value = json.loads(b''.join(chunks))
    if not isinstance(value, dict) or value.get('dag_id') != dag_id or value.get('dag_run_id') != dag_run_id:
        raise ValueError('Airflow run identity differs.')
    return value


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
