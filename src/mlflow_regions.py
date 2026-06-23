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
