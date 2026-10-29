"""Small post-training SHAP reports; never fit a model or select features."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import time
import uuid

import numpy as np
import pandas as pd


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShapConfig:
    enabled: bool = True
    max_samples: int = 32
    background_samples: int = 8
    batch_size: int = 4
    nsamples: int = 64
    max_seconds: int = 60


def resolve_shap_config(config=None):
    if isinstance(config, ShapConfig):
        config = asdict(config)
    if config is not None and not isinstance(config, dict):
        raise ValueError("SHAP configuration must be an object.")
    try:
        options = ShapConfig(**(config or {}))
    except TypeError as error:
        raise ValueError("Unknown SHAP configuration field.") from error
    if type(options.enabled) is not bool:
        raise ValueError("SHAP enabled must be a boolean.")
    for name, upper in (("max_samples", 128), ("background_samples", 64),
                        ("batch_size", 16), ("nsamples", 256), ("max_seconds", 300)):
        value = getattr(options, name)
        if type(value) is not int or not 1 <= value <= upper:
            raise ValueError(f"SHAP {name} must be an integer between 1 and {upper}.")
    return options


def neural_shap_config(config=None):
    """Use a small sequence workload; preserve explicit, validated settings."""
    resolve_shap_config(config)
    values = asdict(config) if isinstance(config, ShapConfig) else dict(config or {})
    return resolve_shap_config({"max_samples": 8, "background_samples": 4,
                                "batch_size": 1, "nsamples": 32, **values})


def _sample_indices(length, limit):
    if length == 0:
        raise ValueError("SHAP training samples are empty.")
    return np.linspace(0, length - 1, min(length, limit), dtype=int)


def _take(values, indices):
    return values.iloc[indices] if hasattr(values, "iloc") else values[indices]


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _values_array(values, expected_shape):
    if isinstance(values, list):
        if len(values) != 1:
            raise ValueError("Only single-output regression is supported.")
        values = values[0]
    values = np.asarray(values, dtype=float)
    if values.shape == (*expected_shape, 1):
        values = values[..., 0]
    if values.shape != expected_shape or not np.isfinite(values).all():
        raise ValueError("Invalid SHAP values or unexpected dimensions.")
    return values


def _importance(names, totals, sample_count):
    scores = totals / sample_count
    total = float(scores.sum())
    return sorted(
        [{"feature": str(name), "mean_abs_shap": float(score),
          "share_percent": float(100 * score / total) if total else 0.0}
         for name, score in zip(names, scores)],
        key=lambda row: (-row["mean_abs_shap"], row["feature"]),
    )


def explain_saved_model(model, training_inputs, *, model_name, model_path,
                        output_path, feature_names=None, target_scale=1.0,
                        context=None, timestamps=None, sequence=False, config=None):
    """Explain training/development samples without touching Final Test.

    Sequence attributions are converted back to traffic units. max_seconds is
    checked between batches, not an OS timeout. Failures preserve the candidate.
    """
    options = neural_shap_config(config) if sequence else resolve_shap_config(config)
    if not options.enabled:
        return {"status": "disabled"}
    started = time.perf_counter()
    report = {
        "schema_version": 1, "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_name": model_name, "context": dict(context or {}),
        "purpose": "model_explanation_only", "usable_for_feature_selection": False,
        "sample_source": "training_development", "final_test_used": False,
        "output_units": "traffic_volume", "configuration": asdict(options),
    }
    try:
        import shap

        report["shap_version"] = shap.__version__
        report["model_sha256"] = file_sha256(model_path)
        indices = _sample_indices(len(training_inputs), options.max_samples)
        sample = _take(training_inputs, indices)
        estimator = model
        if hasattr(model, "named_steps"):
            preprocessor = model.named_steps["preprocessor"]
            sample = preprocessor.transform(sample)
            feature_names = list(preprocessor.get_feature_names_out())
            estimator = model.named_steps["model"]
        elif feature_names is None and hasattr(sample, "columns"):
            feature_names = list(sample.columns)
        sample = np.asarray(sample, dtype=np.float32 if sequence else float)
        names = list(feature_names) if feature_names is not None else []
        if (sample.ndim != (3 if sequence else 2) or len(names) != sample.shape[-1]
                or len(set(names)) != len(names) or not np.isfinite(sample).all()):
            raise ValueError("SHAP inputs do not match the trained feature schema.")
        if not sequence and hasattr(estimator, "feature_names_in_"):
            if names != list(estimator.feature_names_in_):
                raise ValueError("SHAP feature order differs from the saved model.")
        if sequence and tuple(model.input_shape[1:]) != sample.shape[1:]:
            raise ValueError("SHAP sequence shape differs from the saved model.")
        target_scale = float(target_scale)
        if not np.isfinite(target_scale) or target_scale <= 0:
            raise ValueError("Invalid target scale.")
        workload = sample.nbytes + options.batch_size * options.nsamples * sample[0].nbytes
        if workload > 32 * 1024 * 1024:
            raise MemoryError("SHAP sampled workload exceeds 32 MiB.")
        report.update(feature_names=names, sample_count=len(sample),
                      sample_indices=indices.tolist(), target_scale=target_scale)
        if timestamps is not None:
            if len(timestamps) != len(training_inputs):
                raise ValueError("SHAP timestamps do not match training inputs.")
            report["sample_target_timestamps"] = [str(value) for value in _take(timestamps, indices)]

        if sequence:
            background_indices = _sample_indices(len(training_inputs), options.background_samples)
            background = np.asarray(_take(training_inputs, background_indices), dtype=np.float32)
            if background.shape[1:] != sample.shape[1:] or not np.isfinite(background).all():
                raise ValueError("Invalid SHAP sequence background.")
            explainer = shap.GradientExplainer(model, background, batch_size=options.batch_size)
            report.update(explainer="GradientExplainer", approximate=True,
                          background_indices=background_indices.tolist(),
                          sequence_length=int(sample.shape[1]),
                          aggregation="mean_samples_sum_timesteps_abs_shap")
            time_totals = np.zeros(sample.shape[1], dtype=float)
        else:
            explainer = shap.TreeExplainer(estimator, feature_perturbation="tree_path_dependent",
                                           model_output="raw")
            report.update(explainer="TreeExplainer", approximate=False,
                          aggregation="mean_samples_abs_shap")
        totals = np.zeros(len(names), dtype=float)
        max_additivity_error = 0.0
        for offset in range(0, len(sample), options.batch_size):
            if time.perf_counter() - started > options.max_seconds:
                raise TimeoutError("SHAP exceeded its between-batch time budget.")
            batch = sample[offset:offset + options.batch_size]
            if sequence:
                state = np.random.get_state()
                try:
                    values = explainer.shap_values(batch, nsamples=options.nsamples, rseed=42 + offset)
                finally:
                    np.random.set_state(state)
            else:
                values = explainer.shap_values(batch, check_additivity=True)
            values = _values_array(values, batch.shape)
            if time.perf_counter() - started > options.max_seconds:
                raise TimeoutError("SHAP exceeded its between-batch time budget.")
            if not sequence:
                prediction_inputs = pd.DataFrame(batch, columns=names) if hasattr(estimator, "feature_names_in_") else batch
                predicted = np.asarray(estimator.predict(prediction_inputs)).reshape(-1)
                reconstructed = np.asarray(explainer.expected_value).item() + values.sum(axis=1)
                if not np.allclose(predicted, reconstructed, rtol=1e-4, atol=1e-3):
                    raise ValueError("SHAP contributions do not reconstruct predictions.")
                max_additivity_error = max(max_additivity_error, float(np.abs(predicted - reconstructed).max()))
            magnitude = np.abs(values) * target_scale
            if not np.isfinite(magnitude).all():
                raise ValueError("SHAP contributions overflowed target units.")
            if sequence:
                totals += magnitude.sum(axis=(0, 1))
                time_totals += magnitude.sum(axis=(0, 2))
            else:
                totals += magnitude.sum(axis=0)
        report["importance"] = _importance(names, totals, len(sample))
        if sequence:
            report["time_importance"] = [
                {"lag_hours": len(time_totals) - index, "mean_abs_shap": float(value / len(sample))}
                for index, value in enumerate(time_totals)
            ]
        else:
            report["max_additivity_error"] = max_additivity_error
        report["status"] = "completed"
    except Exception as error:
        report["status"] = "unavailable" if isinstance(error, ImportError) else "failed"
        report["error_type"] = type(error).__name__
        logger.warning("SHAP %s for %s (%s)", report["status"], model_name, type(error).__name__)
    report["elapsed_seconds"] = round(time.perf_counter() - started, 4)
    metadata = {key: report[key] for key in ("status", "model_name", "elapsed_seconds", "model_sha256", "error_type") if key in report}
    if sequence and report["context"].get("preprocessor_sha256"):
        metadata["preprocessor_sha256"] = report["context"]["preprocessor_sha256"]
    path = Path(output_path)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
        # The existing bridge already transports and rebinds this documented key.
        metadata["source_report"] = str(path)
        metadata["report_sha256"] = file_sha256(path)
    except (OSError, TypeError, ValueError) as error:
        metadata.update(status="failed", error_type=type(error).__name__)
        logger.warning("SHAP report could not be saved (%s)", type(error).__name__)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return metadata
