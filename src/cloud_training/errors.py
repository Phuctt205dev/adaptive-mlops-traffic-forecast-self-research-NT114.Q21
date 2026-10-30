"""Safe child diagnostics: identifiers/line numbers only, never exception text."""
from pathlib import Path
import re
import traceback

FAILURE_PREFIX = "TRAFFIC_EXECUTION_FAILURE "


def validate_failure(value):
    fields = {"error_type", "source", "line", "final_source", "final_line"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Invalid normalized failure fields.")
    if not isinstance(value["error_type"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", value["error_type"]):
        raise ValueError("Invalid failure identifier.")
    for name in ("source", "final_source"):
        if not isinstance(value[name], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,100}\.py", value[name]):
            raise ValueError("Invalid failure source.")
    for name in ("line", "final_line"):
        if type(value[name]) is not int or not 1 <= value[name] <= 1000000:
            raise ValueError("Invalid failure location.")
    return dict(value)


def safe_failure(error):
    frames = traceback.extract_tb(error.__traceback__)
    root = Path(__file__).resolve().parents[2]
    project = [frame for frame in frames if Path(frame.filename).resolve().is_relative_to(root)]
    source = project[-1] if project else frames[-1] if frames else None
    final = frames[-1] if frames else None
    def filename(frame):
        name = Path(frame.filename).name if frame else "unknown.py"
        return name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,100}\.py", name) else "unknown.py"
    return validate_failure({"error_type": type(error).__name__, "source": filename(source),
        "line": source.lineno if source else 1, "final_source": filename(final), "final_line": final.lineno if final else 1})
