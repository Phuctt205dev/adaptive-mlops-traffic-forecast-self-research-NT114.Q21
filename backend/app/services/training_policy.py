"""Admission rules for new work; historical run metadata stays unchanged."""
from backend.app.core.errors import ApplicationError


def cloud_configuration(configuration):
    if configuration.get("training_backend", "kaggle") != "kaggle":
        raise ApplicationError("local_training_removed", "Training runs on Kaggle. Local training is no longer supported.", 422)
    return {**configuration, "training_backend": "kaggle"}
