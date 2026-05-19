# src/drift.py
from sklearn.metrics import mean_absolute_error
import pandas as pd
import os
from datetime import datetime


def detect_drift_by_mae(
    y_true,
    y_pred,
    mae_threshold=500,
    model_version="unknown"
):
    mae = mean_absolute_error(
        y_true,
        y_pred
    )

    print("\n=== DRIFT CHECK (MAE) ===")
    print(f"Current MAE: {mae:.2f}")
    print(
        f"Threshold : {mae_threshold}"
    )

    # =========================
    # NEW:
    # DRIFT HISTORY LOG
    # =========================
    os.makedirs(
        "monitoring",
        exist_ok=True
    )

    drift_log = (
        "monitoring/drift_history.csv"
    )

    drift_detected = (
        mae > mae_threshold
    )

    new_row = pd.DataFrame(
        [{
            "timestamp":
            datetime.now(),

            "model_version":
            model_version,

            "mae":
            mae,

            "threshold":
            mae_threshold,

            "drift":
            drift_detected
        }]
    )

    if os.path.exists(
        drift_log
    ):
        old = pd.read_csv(
            drift_log
        )

        all_logs = pd.concat(
            [old, new_row],
            ignore_index=True
        )

    else:
        all_logs = new_row

    all_logs.to_csv(
        drift_log,
        index=False
    )

    print(
        "📝 Drift log saved"
    )

    if drift_detected:
        print("🚨 DRIFT DETECTED")
        return True, mae
    else:
        print("✅ NO DRIFT")
        return False, mae