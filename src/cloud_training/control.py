"""Private notebook source identity and expiring cooperative control messages."""
import ast
import hashlib
import math
from pathlib import Path
import time

from src.cloud_training.package import validate_context
from src.cloud_training.probe import canonical

CONTROL_PREFIX = "# TRAFFIC_CONTROL "
LEASE_SECONDS = 180


def body_digest(source):
    lines = source.replace("\r\n", "\n").splitlines()
    body = "\n".join(line for line in lines if not line.startswith((CONTROL_PREFIX, "CONFIG = "))).rstrip() + "\n"
    return hashlib.sha256(body.encode()).hexdigest()


def source_configuration(source):
    lines = [line[len("CONFIG = "):] for line in source.splitlines() if line.startswith("CONFIG = ")]
    if len(lines) != 1:
        raise ValueError("Notebook must have exactly one execution configuration.")
    config = ast.literal_eval(lines[0])
    validate_context(config["context"])
    if config["body_sha256"] != body_digest(source):
        raise ValueError("Notebook supervisor source hash differs.")
    return config


def control_message(attempt_id, *, cancel=False, clock=None):
    current = time.time() if clock is None else clock
    if not isinstance(current, (int, float)) or not math.isfinite(current) or type(cancel) is not bool:
        raise ValueError("Invalid control clock/cancellation state.")
    return {"attempt_id": attempt_id, "cancel": cancel, "lease_until": current + LEASE_SECONDS}


def with_control(source, *, cancel=False, clock=None):
    configuration = source_configuration(source)
    body = "\n".join(line for line in source.replace("\r\n", "\n").splitlines()
                     if not line.startswith(CONTROL_PREFIX)).rstrip() + "\n"
    control = control_message(configuration["context"]["attempt_id"], cancel=cancel, clock=clock)
    return body + CONTROL_PREFIX + canonical(control).decode() + "\n"


def render_notebook(snapshot, reference, dataset_reference, *, clock=None):
    validate_context(snapshot["context"])
    # These are deterministic, per-attempt private resources. Never accept an
    # arbitrary existing notebook as a target for control writes or cleanup.
    attempt = snapshot["context"]["attempt_id"].replace("-", "")
    if (reference.split("/")[-1] != "traffic-train-" + attempt
            or dataset_reference != reference.split("/")[0] + "/traffic-input-" + attempt):
        raise ValueError("Cloud resource identity must be derived from this attempt.")
    template = Path(__file__).with_name("notebook.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    configuration = {**snapshot, "reference": reference, "dataset_reference": dataset_reference,
                     "body_sha256": body_digest(template)}
    source = template.replace("CONFIG = {}", "CONFIG = " + repr(configuration), 1)
    return with_control(source, clock=clock)
