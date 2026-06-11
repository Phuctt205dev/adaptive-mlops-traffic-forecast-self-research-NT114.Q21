import json
import os
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from src.time_series_features import (
    add_calendar_features,
    build_causal_target_series,
    merge_hourly_with_audit,
)
from src.time_series_preprocess import (
    NUMERIC_FEATURE_COLUMNS,
    TARGET_COLUMN,
    TIME_COLUMN,
)


DEFAULT_SEQUENCE_LENGTH = 168
DEFAULT_TRAIN_RATIO = 0.70
DEFAULT_VALIDATION_RATIO = 0.15

# Các feature số tại từng giờ trong cửa sổ 168 giờ.
# traffic_history_value là traffic causal: không dùng thông tin tương lai.
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


def get_eligible_target_indices(
    source_df,
    sequence_length=DEFAULT_SEQUENCE_LENGTH,
):
    """
    Lấy vị trí target có đủ lịch sử và là target quan sát thật.

    Với target ở vị trí t, input là các dòng [t-168, ..., t-1].
    Dòng t tuyệt đối không nằm trong sequence đầu vào của chính nó.
    """
    if sequence_length <= 0:
        raise ValueError("sequence_length phải lớn hơn 0.")

    observed_mask = source_df["target_observed"].to_numpy(dtype=bool)
    candidate_indices = np.arange(sequence_length, len(source_df))
    return candidate_indices[observed_mask[candidate_indices]]


def split_target_indices(
    target_indices,
    train_ratio=DEFAULT_TRAIN_RATIO,
    validation_ratio=DEFAULT_VALIDATION_RATIO,
):
    """Chia vị trí target theo thứ tự thời gian, không shuffle."""
    if train_ratio <= 0 or validation_ratio <= 0:
        raise ValueError("Tỉ lệ train và validation phải lớn hơn 0.")
    if train_ratio + validation_ratio >= 1:
        raise ValueError(
            "Tổng tỉ lệ train và validation phải nhỏ hơn 1."
        )

    target_indices = np.asarray(target_indices, dtype=np.int64)
    train_end = int(len(target_indices) * train_ratio)
    validation_end = train_end + int(
        len(target_indices) * validation_ratio
    )
    if train_end < 1 or validation_end >= len(target_indices):
        raise ValueError("Không đủ sample để chia train/validation/test.")

    return {
        "train": target_indices[:train_end],
        "validation": target_indices[train_end:validation_end],
        "test": target_indices[validation_end:],
    }


def fit_sequence_preprocessors(source_df, train_target_indices):
    """
    Fit scaler và encoder chỉ bằng phần thời gian thuộc train.

    Sequence train cuối cùng kết thúc ở giờ ngay trước target train cuối.
    Validation và test không được tham gia tính mean/std hoặc category.
    """
    train_target_indices = np.asarray(
        train_target_indices,
        dtype=np.int64,
    )
    if train_target_indices.size == 0:
        raise ValueError("Không có target train để fit preprocessing.")

    last_train_history_index = int(train_target_indices[-1] - 1)
    train_history = source_df.iloc[
        : last_train_history_index + 1
    ]

    feature_scaler = StandardScaler()
    feature_scaler.fit(
        train_history[
            SEQUENCE_NUMERIC_COLUMNS
        ].to_numpy(dtype=float)
    )

    weather_encoder = OneHotEncoder(
        handle_unknown="ignore",
        sparse_output=False,
        dtype=np.float32,
    )
    weather_encoder.fit(
        train_history[[SEQUENCE_CATEGORICAL_COLUMN]]
    )

    target_scaler = StandardScaler()
    train_targets = source_df.iloc[train_target_indices][
        [TARGET_COLUMN]
    ].to_numpy(dtype=float)
    target_scaler.fit(train_targets)

    weather_feature_names = list(
        weather_encoder.get_feature_names_out(
            [SEQUENCE_CATEGORICAL_COLUMN]
        )
    )
    return {
        "feature_scaler": feature_scaler,
        "weather_encoder": weather_encoder,
        "target_scaler": target_scaler,
        "numeric_feature_names": list(SEQUENCE_NUMERIC_COLUMNS),
        "weather_feature_names": weather_feature_names,
        "sequence_feature_names": (
            list(SEQUENCE_NUMERIC_COLUMNS)
            + weather_feature_names
        ),
        "last_train_history_time": source_df.iloc[
            last_train_history_index
        ][TIME_COLUMN],
    }


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
    return np.concatenate(
        [numeric_values, weather_values],
        axis=1,
    )


def build_sequences(
    transformed_timeline,
    source_df,
    target_indices,
    target_scaler,
    sequence_length=DEFAULT_SEQUENCE_LENGTH,
):
    """
    Đóng gói dữ liệu thành tensor 3 chiều cho LSTM.

    X có shape: (samples, 168, features).
    y có shape: (samples, 1).
    """
    target_indices = np.asarray(target_indices, dtype=np.int64)
    feature_count = transformed_timeline.shape[1]
    sequences = np.empty(
        (len(target_indices), sequence_length, feature_count),
        dtype=np.float32,
    )

    for output_index, target_index in enumerate(target_indices):
        start_index = target_index - sequence_length
        sequences[output_index] = transformed_timeline[
            start_index:target_index
        ]

    raw_targets = source_df.iloc[target_indices][
        [TARGET_COLUMN]
    ].to_numpy(dtype=np.float32)
    scaled_targets = target_scaler.transform(
        raw_targets
    ).astype(np.float32)
    timestamps = source_df.iloc[target_indices][
        TIME_COLUMN
    ].to_numpy(dtype="datetime64[ns]")
    return sequences, scaled_targets, raw_targets, timestamps


def create_lstm_sequence_datasets(
    hourly_df,
    audit_df,
    sequence_length=DEFAULT_SEQUENCE_LENGTH,
    train_ratio=DEFAULT_TRAIN_RATIO,
    validation_ratio=DEFAULT_VALIDATION_RATIO,
):
    """Tạo train/validation/test tensor và preprocessing artifact."""
    source = prepare_sequence_source(hourly_df, audit_df)
    eligible_indices = get_eligible_target_indices(
        source,
        sequence_length=sequence_length,
    )
    split_indices = split_target_indices(
        eligible_indices,
        train_ratio=train_ratio,
        validation_ratio=validation_ratio,
    )
    preprocessors = fit_sequence_preprocessors(
        source,
        split_indices["train"],
    )
    transformed_timeline = transform_sequence_source(
        source,
        preprocessors,
    )

    datasets = {}
    for split_name, indices in split_indices.items():
        X, y, raw_y, timestamps = build_sequences(
            transformed_timeline,
            source,
            indices,
            preprocessors["target_scaler"],
            sequence_length=sequence_length,
        )
        datasets[split_name] = {
            "X": X,
            "y": y,
            "raw_y": raw_y,
            "timestamps": timestamps,
        }

    report = build_sequence_report(
        source,
        datasets,
        preprocessors,
        sequence_length,
        train_ratio,
        validation_ratio,
    )
    return datasets, preprocessors, report


def _split_summary(split_data):
    timestamps = split_data["timestamps"]
    return {
        "samples": int(len(timestamps)),
        "start": pd.Timestamp(timestamps[0]).isoformat(),
        "end": pd.Timestamp(timestamps[-1]).isoformat(),
        "X_shape": list(split_data["X"].shape),
        "y_shape": list(split_data["y"].shape),
    }


def build_sequence_report(
    source_df,
    datasets,
    preprocessors,
    sequence_length,
    train_ratio,
    validation_ratio,
):
    """Tạo báo cáo để kiểm tra shape, split và chính sách chống leakage."""
    return {
        "created_at": datetime.now().isoformat(),
        "source_hourly_rows": int(len(source_df)),
        "sequence_length_hours": int(sequence_length),
        "feature_count_per_hour": int(
            len(preprocessors["sequence_feature_names"])
        ),
        "sequence_feature_names": preprocessors[
            "sequence_feature_names"
        ],
        "splits": {
            name: _split_summary(split_data)
            for name, split_data in datasets.items()
        },
        "train_ratio": float(train_ratio),
        "validation_ratio": float(validation_ratio),
        "test_ratio": round(
            1 - train_ratio - validation_ratio,
            6,
        ),
        "feature_scaler_fit_end": pd.Timestamp(
            preprocessors["last_train_history_time"]
        ).isoformat(),
        "scaler_policy": (
            "Feature scaler, weather encoder và target scaler "
            "chỉ fit bằng train"
        ),
        "label_policy": "Chỉ target_observed=True được dùng làm y",
        "window_policy": (
            "Sequence của target t dùng các giờ [t-168, ..., t-1]"
        ),
        "traffic_policy": (
            "traffic_history_value chỉ dùng target thật hoặc fallback quá khứ"
        ),
    }


def save_lstm_sequence_artifacts(
    datasets,
    preprocessors,
    report,
    output_npz_path,
    output_preprocessor_path,
    output_report_path,
):
    """Lưu tensor nén, scaler/encoder và báo cáo bằng file tạm."""
    for path in [
        output_npz_path,
        output_preprocessor_path,
        output_report_path,
    ]:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    temporary_npz_path = f"{output_npz_path}.tmp.npz"
    np.savez_compressed(
        temporary_npz_path,
        X_train=datasets["train"]["X"],
        y_train=datasets["train"]["y"],
        raw_y_train=datasets["train"]["raw_y"],
        timestamps_train=datasets["train"]["timestamps"],
        X_validation=datasets["validation"]["X"],
        y_validation=datasets["validation"]["y"],
        raw_y_validation=datasets["validation"]["raw_y"],
        timestamps_validation=datasets["validation"]["timestamps"],
        X_test=datasets["test"]["X"],
        y_test=datasets["test"]["y"],
        raw_y_test=datasets["test"]["raw_y"],
        timestamps_test=datasets["test"]["timestamps"],
    )
    os.replace(temporary_npz_path, output_npz_path)

    temporary_preprocessor_path = (
        f"{output_preprocessor_path}.tmp"
    )
    joblib.dump(preprocessors, temporary_preprocessor_path)
    os.replace(
        temporary_preprocessor_path,
        output_preprocessor_path,
    )

    temporary_report_path = f"{output_report_path}.tmp"
    with open(
        temporary_report_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(report, file, indent=4, ensure_ascii=False)
    os.replace(temporary_report_path, output_report_path)
