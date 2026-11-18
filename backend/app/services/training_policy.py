"""Backend policy shared by admission and workers; history remains immutable."""
import re

from backend.app.core.application_error import ApplicationError


def cloud_configuration(configuration):
    backend = configuration.get("training_backend", "kaggle")
    if backend not in {"kaggle", "local"}:
        raise ApplicationError("training_backend_invalid", "Choose Kaggle or Local training.", 422)
    if backend != "kaggle" and (configuration.get("trigger_source") == "feature_drift"
                                or configuration.get("retrain_source_model_id")):
        raise ApplicationError("retrain_requires_kaggle", "Retraining always runs on Kaggle.", 422)
    return {**configuration, "training_backend": backend}


def local_ready(settings):
    return bool(getattr(settings, "training_control_enabled", False) and getattr(settings, "local_training_enabled", False)
                and re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", settings.local_training_image)
                and 512 <= settings.local_training_memory_mib <= 6144
                and 1 <= settings.local_training_cpu <= 2
                and settings.local_training_reserve_mib >= 1024
                and 60 <= settings.local_training_deadline_seconds <= 86400)


def require_backend(settings, configuration):
    backend = cloud_configuration(configuration)["training_backend"]
    ready = local_ready(settings) if backend == "local" else settings.training_control_enabled and settings.cloud_training_enabled
    if not ready:
        raise ApplicationError("local_training_not_ready" if backend == "local" else "cloud_training_not_ready",
                               f"{backend.title()} training is not configured on this system.", 503)
    return backend
