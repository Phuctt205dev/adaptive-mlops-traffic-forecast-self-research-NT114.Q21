"""Bounded, normalized timing facts. No payloads, credentials or cross-clock subtraction."""
from datetime import datetime
import math

SOURCE_CLOCKS = {"ml_child_monotonic_v1", "supervisor_monotonic_v1", "runner_monotonic_v1"}
FIT_PURPOSES = {"cv_full", "cv_reduced", "cv_reused", "final"}
TIME_FIELDS = {"source_time", "source_clock", "source_elapsed_seconds"}
RELAY_FIELDS = {"supervisor_relay_time", "worker_stream_time"}


def rejection_summary(value):
    fallback = {"reason": "provider_rejected", "invalid_sources": [], "retryable": False}
    if not isinstance(value, dict):
        return fallback
    reasons = {"provider_rejected", "authentication", "quota", "invalid_sources", "rate_limit", "temporarily_unavailable"}
    reason = value.get("reason")
    if not isinstance(reason, str) or reason not in reasons:
        return fallback
    invalid = value.get("invalid_sources", [])
    allowed = {"invalid_dataset_sources", "invalid_kernel_sources", "invalid_competition_sources", "invalid_model_sources"}
    if not isinstance(invalid, list) or len(invalid) > 4 or any(not isinstance(item, str) or item not in allowed for item in invalid):
        return fallback
    if invalid:
        reason = "invalid_sources"
    return {"reason": reason, "invalid_sources": sorted(set(invalid)),
            "retryable": reason in {"rate_limit", "temporarily_unavailable"} and value.get("retryable") is True}


def source_timing(event):
    present = TIME_FIELDS.intersection(event)
    if present and present != TIME_FIELDS:
        raise ValueError("Incomplete source timing.")
    if present:
        elapsed = event["source_elapsed_seconds"]
        if (not isinstance(event["source_clock"], str) or event["source_clock"] not in SOURCE_CLOCKS
                or type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= 7 * 86400):
            raise ValueError("Invalid source timing.")
    for key in ({"source_time"} | RELAY_FIELDS).intersection(event):
        value = event[key]
        if not isinstance(value, str) or len(value) > 40 or datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("Timestamp needs a timezone.")
    return {key: event[key] for key in (TIME_FIELDS | RELAY_FIELDS).intersection(event)}


def operation_timing(telemetry, name, seconds, *, failed=False, byte_count=0):
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Invalid operation duration.")
    operations = telemetry.setdefault("operations", {})
    if name not in operations and len(operations) >= 24:
        raise ValueError("Operation timing budget exceeded.")
    item = operations.setdefault(name, {"count": 0, "seconds": 0.0, "errors": 0, "bytes": 0})
    item.update(count=item["count"] + 1, seconds=round(item["seconds"] + seconds, 6),
                errors=item["errors"] + int(failed), bytes=item["bytes"] + byte_count,
                successful_bytes=item.get("successful_bytes", 0) + (0 if failed else byte_count))
    telemetry.update(version=1, duration_clock="worker_monotonic", operations=operations)


def timing_summary(provider):
    """Never expose provider source, resource references or signed download URLs."""
    return {key: provider[key] for key in ("phase", "telemetry", "last_failure", "provider_state",
            "provider_observed_at", "provider_checked_at", "last_control_confirmed_at", "workload") if key in provider}
