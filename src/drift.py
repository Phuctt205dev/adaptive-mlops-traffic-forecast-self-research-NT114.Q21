import os
import pandas as pd
from datetime import datetime
from sklearn.metrics import mean_absolute_error


# =========================
# ENSURE LOG FOLDER
# =========================
def ensure_monitoring_folder():
    os.makedirs("monitoring", exist_ok=True)


def get_historical_mae_baseline(
    model_version,
    history_size=6,
    minimum_windows=3,
    log_path="monitoring/drift_history.csv",
):
    """
    Return the median MAE of recent production windows for one champion.

    A new champion has no trustworthy production baseline yet. Ratio-based
    drift remains disabled until enough windows have been observed.
    """
    if not model_version or str(model_version).lower() == "unknown":
        return None

    if not os.path.exists(log_path):
        return None

    history = pd.read_csv(log_path)
    required_columns = {"model_version", "current_mae"}
    if not required_columns.issubset(history.columns):
        return None

    model_history = history[
        history["model_version"].astype(str) == str(model_version)
    ].copy()

    # Drifted windows describe abnormal behavior and must not redefine what
    # "normal" means. Old logs without this column remain supported.
    if "drift" in model_history.columns:
        stable_values = {"false", "0", "0.0"}
        model_history = model_history[
            model_history["drift"]
            .astype(str)
            .str.lower()
            .isin(stable_values)
        ]

    model_history["current_mae"] = pd.to_numeric(
        model_history["current_mae"],
        errors="coerce",
    )
    model_history = model_history.dropna(subset=["current_mae"])

    recent_maes = model_history["current_mae"].tail(history_size)
    if len(recent_maes) < minimum_windows:
        return None

    return float(recent_maes.median())


# =========================
# DETECT DRIFT BY MAE
# =========================
def detect_drift_by_mae(
    y_true,
    y_pred,
    mae_threshold=700.0,
    model_version="unknown",
    baseline_mae=None,
    degradation_ratio=1.5,
    log_path="monitoring/drift_history.csv"
):
    """Detect drift using a fixed limit and a production-history baseline."""

    ensure_monitoring_folder()

    current_mae = mean_absolute_error(
        y_true,
        y_pred
    )

    fixed_threshold = float(
        mae_threshold
    )

    drift_by_fixed_threshold = (
        current_mae > fixed_threshold
    )

    ratio_threshold = None
    drift_by_degradation_ratio = False

    if baseline_mae is not None:
        try:
            baseline_mae = float(
                baseline_mae
            )

            ratio_threshold = (
                baseline_mae * float(degradation_ratio)
            )

            drift_by_degradation_ratio = (
                current_mae > ratio_threshold
            )

        except Exception:
            baseline_mae = None
            ratio_threshold = None
            drift_by_degradation_ratio = False

    drift = (
        drift_by_fixed_threshold
        or
        drift_by_degradation_ratio
    )

    print(
        "\n=== DRIFT CHECK (MAE) ===",
        flush=True
    )

    print(
        f"Current MAE       : {current_mae:.2f}",
        flush=True
    )

    print(
        f"Fixed threshold   : {fixed_threshold:.2f}",
        flush=True
    )

    if baseline_mae is not None:
        print(
            f"Baseline MAE      : {baseline_mae:.2f}",
            flush=True
        )

        print(
            f"Degradation ratio : {degradation_ratio}",
            flush=True
        )

        print(
            f"Ratio threshold   : {ratio_threshold:.2f}",
            flush=True
        )
    else:
        print(
            "Baseline MAE      : None",
            flush=True
        )

        print(
            "Ratio threshold   : Disabled",
            flush=True
        )

    print(
        f"Drift by fixed    : {drift_by_fixed_threshold}",
        flush=True
    )

    print(
        f"Drift by ratio    : {drift_by_degradation_ratio}",
        flush=True
    )

    if drift:
        print(
            "🚨 DRIFT DETECTED",
            flush=True
        )
    else:
        print(
            "✅ NO DRIFT",
            flush=True
        )

    log_row = {
        "timestamp": datetime.now().isoformat(),
        "model_version": model_version,
        "current_mae": round(float(current_mae), 4),
        "fixed_threshold": round(float(fixed_threshold), 4),
        "baseline_mae": (
            round(float(baseline_mae), 4)
            if baseline_mae is not None
            else None
        ),
        "degradation_ratio": float(degradation_ratio),
        "ratio_threshold": (
            round(float(ratio_threshold), 4)
            if ratio_threshold is not None
            else None
        ),
        "drift_by_fixed_threshold": bool(
            drift_by_fixed_threshold
        ),
        "drift_by_degradation_ratio": bool(
            drift_by_degradation_ratio
        ),
        "drift": bool(
            drift
        )
    }

    if os.path.exists(
        log_path
    ):
        history_df = pd.read_csv(
            log_path
        )

        history_df = pd.concat(
            [
                history_df,
                pd.DataFrame([log_row])
            ],
            ignore_index=True
        )
    else:
        history_df = pd.DataFrame(
            [log_row]
        )

    history_df.to_csv(
        log_path,
        index=False
    )

    print(
        "📝 Drift log saved",
        flush=True
    )

    return drift, current_mae
