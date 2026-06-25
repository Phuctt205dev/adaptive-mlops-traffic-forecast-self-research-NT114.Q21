import re


def sanitize_experiment_component(value):
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[\\/]+", "-", text)
    return text or "Unknown Region"


def region_experiment_name(region_name=None, region_id=None):
    label = sanitize_experiment_component(region_name or region_id)
    return f"Traffic Forecast - {label}"


def legacy_region_experiment_name(region_id):
    return f"Traffic Forecast - Region {region_id}"


def set_mlflow_experiment(mlflow_module, experiment_name):
    try:
        mlflow_module.set_experiment(experiment_name)
        return
    except Exception as error:
        if "deleted experiment" not in str(error).lower():
            raise

    from mlflow.entities import ViewType
    from mlflow.tracking import MlflowClient

    tracking_uri = None
    get_tracking_uri = getattr(mlflow_module, "get_tracking_uri", None)
    if callable(get_tracking_uri):
        tracking_uri = get_tracking_uri()
    client = MlflowClient(tracking_uri=tracking_uri) if tracking_uri else MlflowClient()
    for experiment in client.search_experiments(view_type=ViewType.ALL):
        if experiment.name == experiment_name and experiment.lifecycle_stage == "deleted":
            client.restore_experiment(experiment.experiment_id)
            break
    mlflow_module.set_experiment(experiment_name)
