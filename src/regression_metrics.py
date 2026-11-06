"""Pure metrics shared by serving and training; no model builders."""
import numpy as np
from sklearn.metrics import mean_absolute_error,mean_absolute_percentage_error,mean_squared_error,r2_score


def calculate_regression_metrics(actual, predictions):
    """Tính các metric dùng chung khi so sánh model."""
    actual_array = np.asarray(actual, dtype=float)
    prediction_array = np.asarray(predictions, dtype=float)
    mae = mean_absolute_error(actual_array, prediction_array)
    rmse = np.sqrt(
        mean_squared_error(actual_array, prediction_array)
    )
    mape = (
        mean_absolute_percentage_error(
            actual_array,
            prediction_array,
        )
        * 100
    )
    actual_sum = np.abs(actual_array).sum()
    wape = (
        np.abs(actual_array - prediction_array).sum()
        / actual_sum
        * 100
        if actual_sum > 0
        else 0.0
    )
    return {
        "MAE": round(float(mae), 4),
        "RMSE": round(float(rmse), 4),
        "MAPE": round(float(mape), 4),
        "WAPE": round(float(wape), 4),
        "R2": round(
            float(r2_score(actual_array, prediction_array)),
            6,
        ),
    }
