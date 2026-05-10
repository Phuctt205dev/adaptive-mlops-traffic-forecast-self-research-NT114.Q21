# src/drift.py
from sklearn.metrics import mean_absolute_error


def detect_drift_by_mae(
    y_true,
    y_pred,
    mae_threshold=500
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

    if mae > mae_threshold:
        print("🚨 DRIFT DETECTED")
        return True, mae
    else:
        print("✅ NO DRIFT")
        return False, mae