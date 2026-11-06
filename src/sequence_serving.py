"""Transform saved neural preprocessors; never fit or write artifacts."""
import numpy as np
from src.time_series_preprocess import NUMERIC_FEATURE_COLUMNS
from src.time_series_features import add_calendar_features,build_causal_target_series,merge_hourly_with_audit

SEQUENCE_NUMERIC_COLUMNS = [
    *NUMERIC_FEATURE_COLUMNS,
    "traffic_history_value",
    "traffic_history_observed",
    "is_holiday_binary",
    "is_weekend",
    "hour_sin",
    "hour_cos",
    "day_of_week_sin",
    "day_of_week_cos",
    "month_sin",
    "month_cos",
]

SEQUENCE_CATEGORICAL_COLUMN = "weather_type"


def prepare_sequence_source(hourly_df, audit_df):
    """Tạo bảng liên tục, causal và chỉ gồm feature biết được theo thời gian."""
    merged = merge_hourly_with_audit(hourly_df, audit_df)
    source = add_calendar_features(merged)

    # Không dùng traffic_volume suy luận offline làm lịch sử cho LSTM.
    source["traffic_history_value"] = build_causal_target_series(
        source
    )
    source["traffic_history_observed"] = (
        source["target_observed"].astype(int)
    )
    source[SEQUENCE_CATEGORICAL_COLUMN] = (
        source[SEQUENCE_CATEGORICAL_COLUMN]
        .fillna("Unknown")
        .astype(str)
    )
    return source


def transform_sequence_source(source_df, preprocessors):
    """Áp dụng đúng scaler/encoder đã học từ train cho toàn bộ timeline."""
    numeric_values = preprocessors["feature_scaler"].transform(
        source_df[
            SEQUENCE_NUMERIC_COLUMNS
        ].to_numpy(dtype=float)
    ).astype(np.float32)
    weather_values = preprocessors["weather_encoder"].transform(
        source_df[[SEQUENCE_CATEGORICAL_COLUMN]]
    ).astype(np.float32)
    values = np.concatenate(
        [numeric_values, weather_values],
        axis=1,
    )
    indices = preprocessors.get("selected_feature_indices")
    if indices is not None:
        if (not isinstance(indices, list) or not indices or len(indices) != len(set(indices))
                or any(type(i) is not int or i < 0 or i >= values.shape[1] for i in indices)
                or len(indices) != len(preprocessors["sequence_feature_names"])):
            raise ValueError("Invalid saved sequence feature selection.")
        values = values[:, indices]
    return values
