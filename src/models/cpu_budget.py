"""Job CPU budget is opt-in; existing Kaggle/inference defaults are preserved."""
import os


def training_threads():
    value = os.environ.get("TRAFFIC_LOCAL_CPU_THREADS")
    if value is None:
        return -1
    if value not in {"1", "2"}:
        raise ValueError("Local training requires a CPU budget of one or two threads.")
    return int(value)
