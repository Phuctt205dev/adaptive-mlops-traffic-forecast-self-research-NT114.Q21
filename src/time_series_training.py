import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    mean_absolute_error,
    mean_absolute_percentage_error,
    mean_squared_error,
    r2_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from src.models import build_autoregressive_model
from src.time_series_preprocess import TARGET_COLUMN, TIME_COLUMN


DEFAULT_TRAIN_RATIO = 0.70
DEFAULT_VALIDATION_RATIO = 0.15
DEFAULT_RANDOM_STATE = 42

CATEGORICAL_COLUMNS = [
    "is_holiday",
    "weather_type",
    "weather_description",
]

BASELINE_COLUMNS = {
    "NaiveLag1Hour": "traffic_volume_lag_1h",
    "NaiveLag24Hours": "traffic_volume_lag_24h",
    "NaiveLag168Hours": "traffic_volume_lag_168h",
}


def validate_feature_data(dataframe):
    """Kiểm tra các cột tối thiểu của bảng feature time series."""
    required_columns = {
        TIME_COLUMN,
        TARGET_COLUMN,
        *CATEGORICAL_COLUMNS,
        *BASELINE_COLUMNS.values(),
    }
    missing_columns = sorted(required_columns - set(dataframe.columns))
    if missing_columns:
        raise ValueError(
            "Dữ liệu feature thiếu các cột: "
            + ", ".join(missing_columns)
        )
    if dataframe.empty:
        raise ValueError("Dữ liệu feature đang rỗng.")


def prepare_feature_data(dataframe):
    """Parse thời gian, sắp xếp và loại lỗi dữ liệu nghiêm trọng."""
    validate_feature_data(dataframe)
    result = dataframe.copy()
    result[TIME_COLUMN] = pd.to_datetime(
        result[TIME_COLUMN],
        errors="raise",
    )
    result = result.replace({pd.NA: np.nan})
    result = result.sort_values(TIME_COLUMN).reset_index(drop=True)

    if result[TIME_COLUMN].duplicated().any():
        raise ValueError("Dữ liệu feature có date_time bị trùng.")
    if result[TARGET_COLUMN].isna().any():
        raise ValueError("Target traffic_volume có giá trị thiếu.")
    return result


def split_time_series_data(
    dataframe,
    train_ratio=DEFAULT_TRAIN_RATIO,
    validation_ratio=DEFAULT_VALIDATION_RATIO,
):
    """
    Hàm chia tỷ lệ cũ, chỉ giữ để tương thích với test/module tiện ích.

    Pipeline chính hiện dùng các mốc ngày cố định trong time_series_splits.py.
    """
    if train_ratio <= 0 or validation_ratio <= 0:
        raise ValueError("Tỉ lệ train và validation phải lớn hơn 0.")
    if train_ratio + validation_ratio >= 1:
        raise ValueError(
            "Tổng tỉ lệ train và validation phải nhỏ hơn 1."
        )

    ordered = prepare_feature_data(dataframe)
    row_count = len(ordered)
    train_end = int(row_count * train_ratio)
    validation_end = train_end + int(
        row_count * validation_ratio
    )
    if train_end < 2 or validation_end >= row_count:
        raise ValueError("Dữ liệu quá ít để chia thành ba tập.")

    train_df = ordered.iloc[:train_end].copy()
    validation_df = ordered.iloc[
        train_end:validation_end
    ].copy()
    test_df = ordered.iloc[validation_end:].copy()
    if validation_df.empty or test_df.empty:
        raise ValueError("Validation hoặc test đang rỗng.")
    return train_df, validation_df, test_df


def split_features_and_target(dataframe):
    """Tách timestamp, feature X và target y."""
    timestamps = dataframe[TIME_COLUMN].copy()
    target = dataframe[TARGET_COLUMN].astype(float).copy()
    features = dataframe.drop(
        columns=[TIME_COLUMN, TARGET_COLUMN]
    ).copy()
    return features, target, timestamps


def build_preprocessor(feature_columns):
    """Tạo encoder/scaler; chúng chỉ được fit khi pipeline.fit(train)."""
    categorical_columns = [
        column
        for column in CATEGORICAL_COLUMNS
        if column in feature_columns
    ]
    numeric_columns = [
        column
        for column in feature_columns
        if column not in categorical_columns
    ]

    categorical_pipeline = Pipeline(
        steps=[
            (
                "imputer",
                SimpleImputer(
                    strategy="constant",
                    fill_value="None",
                ),
            ),
            (
                "one_hot",
                OneHotEncoder(
                    handle_unknown="ignore",
                    sparse_output=False,
                ),
            ),
        ]
    )
    numeric_pipeline = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
        ]
    )
    preprocessor = ColumnTransformer(
        transformers=[
            (
                "categorical",
                categorical_pipeline,
                categorical_columns,
            ),
            ("numeric", numeric_pipeline, numeric_columns),
        ],
        remainder="drop",
        verbose_feature_names_out=False,
    )
    return preprocessor.set_output(transform="pandas")


def build_training_pipeline(
    model_name,
    feature_columns,
    random_state=DEFAULT_RANDOM_STATE,
):
    """Đóng gói preprocessing và model autoregressive trong một artifact."""
    return Pipeline(
        steps=[
            ("preprocessor", build_preprocessor(feature_columns)),
            (
                "model",
                build_autoregressive_model(
                    model_name,
                    random_state,
                ),
            ),
        ]
    )


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


def evaluate_baselines(dataframe):
    """Đo các quy tắc lag đơn giản để kiểm tra model có thực sự tốt hơn."""
    return {
        baseline_name: calculate_regression_metrics(
            dataframe[TARGET_COLUMN],
            dataframe[column],
        )
        for baseline_name, column in BASELINE_COLUMNS.items()
    }
