import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

from src.time_series_preprocess import (
    ORIGINAL_COLUMN_ORDER,
    TARGET_COLUMN,
    TIME_COLUMN,
)


DEFAULT_LAG_HOURS = (1, 2, 3, 6, 12, 24, 48, 168)
DEFAULT_ROLLING_WINDOWS = (3, 6, 12, 24, 168)

REQUIRED_AUDIT_COLUMNS = [
    TIME_COLUMN,
    "target_observed",
]


def build_causal_target_series(dataframe):
    """
    Tạo chuỗi traffic chỉ dùng thông tin đã xuất hiện trong quá khứ.

    Target suy luận offline của Giai đoạn 1 có thể tham khảo dữ liệu phía sau,
    vì vậy không được đưa thẳng vào lag hoặc sequence dùng để train model.
    """
    observed_target = dataframe[TARGET_COLUMN].where(
        dataframe["target_observed"]
    )
    causal_target = observed_target.copy()
    causal_target = causal_target.fillna(observed_target.shift(168))
    causal_target = causal_target.fillna(observed_target.shift(24))
    causal_target = causal_target.fillna(
        observed_target
        .shift(1)
        .rolling(window=168, min_periods=1)
        .median()
    )
    causal_target = causal_target.ffill()

    if causal_target.isna().any():
        raise ValueError(
            "Không thể tạo causal target vì đầu chuỗi không có target thật."
        )
    return causal_target


def _validate_columns(dataframe, required_columns, dataframe_name):
    """Báo rõ các cột còn thiếu thay vì để pandas sinh lỗi khó hiểu."""
    missing_columns = [
        column
        for column in required_columns
        if column not in dataframe.columns
    ]
    if missing_columns:
        raise ValueError(
            f"{dataframe_name} thiếu các cột: "
            + ", ".join(missing_columns)
        )


def _prepare_time_index(dataframe, dataframe_name):
    """Đổi date_time sang datetime, sắp xếp và kiểm tra trùng thời điểm."""
    result = dataframe.copy()
    result[TIME_COLUMN] = pd.to_datetime(
        result[TIME_COLUMN],
        errors="raise",
    )
    result = result.sort_values(TIME_COLUMN).reset_index(drop=True)

    if result[TIME_COLUMN].duplicated().any():
        raise ValueError(
            f"{dataframe_name} có date_time bị trùng."
        )
    return result


def _validate_hourly_frequency(hourly_df):
    """Đảm bảo mỗi dòng cách dòng trước đúng một giờ."""
    time_differences = hourly_df[TIME_COLUMN].diff().dropna()
    invalid_differences = time_differences[
        time_differences != pd.Timedelta(hours=1)
    ]
    if not invalid_differences.empty:
        raise ValueError(
            "Dữ liệu chưa liên tục theo giờ. "
            "Hãy chạy 'python -m scripts.data.prepare_hourly_data' trước."
        )


def merge_hourly_with_audit(hourly_df, audit_df):
    """Ghép dữ liệu chính với thông tin target thật bằng date_time."""
    _validate_columns(
        hourly_df,
        ORIGINAL_COLUMN_ORDER,
        "CSV hourly",
    )
    _validate_columns(
        audit_df,
        REQUIRED_AUDIT_COLUMNS,
        "CSV audit",
    )

    hourly = _prepare_time_index(hourly_df, "CSV hourly")
    audit = _prepare_time_index(audit_df, "CSV audit")
    _validate_hourly_frequency(hourly)

    if set(hourly[TIME_COLUMN]) != set(audit[TIME_COLUMN]):
        raise ValueError(
            "CSV hourly và CSV audit không có cùng tập date_time."
        )

    merged = hourly.merge(
        audit,
        on=TIME_COLUMN,
        how="left",
        validate="one_to_one",
    )
    merged["target_observed"] = (
        merged["target_observed"]
        .astype(str)
        .str.lower()
        .isin(["true", "1", "1.0"])
    )
    return merged


def add_calendar_features(dataframe):
    """Tạo feature lịch để model hiểu giờ, thứ, tháng và tính tuần hoàn."""
    result = dataframe.copy()
    date_time = result[TIME_COLUMN]

    result["hour"] = date_time.dt.hour
    result["day_of_week"] = date_time.dt.dayofweek
    result["day_of_month"] = date_time.dt.day
    result["month"] = date_time.dt.month
    result["day_of_year"] = date_time.dt.dayofyear
    result["is_weekend"] = result["day_of_week"].isin([5, 6]).astype(int)
    result["is_holiday_binary"] = (
        result["is_holiday"].notna().astype(int)
    )

    # Sin/cos đặt các điểm cuối chu kỳ gần nhau, ví dụ 23 giờ gần 0 giờ.
    result["hour_sin"] = np.sin(2 * np.pi * result["hour"] / 24)
    result["hour_cos"] = np.cos(2 * np.pi * result["hour"] / 24)
    result["day_of_week_sin"] = np.sin(
        2 * np.pi * result["day_of_week"] / 7
    )
    result["day_of_week_cos"] = np.cos(
        2 * np.pi * result["day_of_week"] / 7
    )
    result["month_sin"] = np.sin(
        2 * np.pi * (result["month"] - 1) / 12
    )
    result["month_cos"] = np.cos(
        2 * np.pi * (result["month"] - 1) / 12
    )
    return result


def add_target_history_features(
    dataframe,
    lag_hours=DEFAULT_LAG_HOURS,
    rolling_windows=DEFAULT_ROLLING_WINDOWS,
):
    """
    Tạo lag và rolling chỉ từ quá khứ.

    shift(1) là hàng rào chống data leakage: target của giờ hiện tại không
    được tham gia vào feature dùng để dự đoán chính giờ đó.
    """
    result = dataframe.copy()

    # Không dùng target suy luận của Giai đoạn 1 vì cách suy luận offline có thể
    # tham khảo dữ liệu ở phía sau. Chuỗi dưới đây chỉ lấy thông tin đã xuất
    # hiện trong quá khứ tại thời điểm dự báo.
    causal_target = build_causal_target_series(result)

    past_target = causal_target.shift(1)
    past_observed = result["target_observed"].shift(1)

    for hours in lag_hours:
        result[f"traffic_volume_lag_{hours}h"] = (
            causal_target.shift(hours)
        )
        result[f"lag_{hours}h_target_observed"] = (
            result["target_observed"]
            .shift(hours)
            .eq(True)
            .astype(int)
        )

    for window in rolling_windows:
        rolling_target = past_target.rolling(
            window=window,
            min_periods=window,
        )
        prefix = f"traffic_volume_rolling_{window}h"
        result[f"{prefix}_mean"] = rolling_target.mean()
        result[f"{prefix}_median"] = rolling_target.median()
        result[f"{prefix}_std"] = rolling_target.std(ddof=0)
        result[f"{prefix}_min"] = rolling_target.min()
        result[f"{prefix}_max"] = rolling_target.max()
        result[f"history_observed_ratio_{window}h"] = (
            past_observed
            .rolling(window=window, min_periods=window)
            .mean()
        )

    return result


def build_feature_report(
    merged_df,
    feature_df,
    lag_hours,
    rolling_windows,
):
    """Tóm tắt số dòng bị loại và các feature đã tạo."""
    return {
        "created_at": datetime.now().isoformat(),
        "source_rows": int(len(merged_df)),
        "output_training_rows": int(len(feature_df)),
        "removed_unobserved_target_rows": int(
            (~merged_df["target_observed"]).sum()
        ),
        "removed_warmup_or_incomplete_rows": int(
            len(merged_df[merged_df["target_observed"]])
            - len(feature_df)
        ),
        "lag_hours": list(lag_hours),
        "rolling_windows": list(rolling_windows),
        "largest_required_history_hours": int(
            max((*lag_hours, *rolling_windows))
        ),
        "feature_columns": [
            column
            for column in feature_df.columns
            if column not in [TIME_COLUMN, TARGET_COLUMN]
        ],
        "target_column": TARGET_COLUMN,
        "label_policy": "Chỉ giữ target_observed=True",
        "leakage_policy": (
            "Lag/rolling chỉ dùng target quan sát hoặc giá trị điền từ quá khứ"
        ),
        "history_imputation_policy": (
            "Ưu tiên target quan sát 168h trước, 24h trước, "
            "sau đó median tối đa 168h quá khứ"
        ),
    }


def create_time_series_features(
    hourly_df,
    audit_df,
    lag_hours=DEFAULT_LAG_HOURS,
    rolling_windows=DEFAULT_ROLLING_WINDOWS,
):
    """Tạo bảng feature có thể dùng cho mô hình tree-based time series."""
    lag_hours = tuple(sorted(set(int(value) for value in lag_hours)))
    rolling_windows = tuple(
        sorted(set(int(value) for value in rolling_windows))
    )
    if not lag_hours or not rolling_windows:
        raise ValueError("Danh sách lag và rolling window không được rỗng.")
    if min((*lag_hours, *rolling_windows)) <= 0:
        raise ValueError("Lag và rolling window phải lớn hơn 0.")

    merged = merge_hourly_with_audit(hourly_df, audit_df)
    featured = add_calendar_features(merged)
    featured = add_target_history_features(
        featured,
        lag_hours=lag_hours,
        rolling_windows=rolling_windows,
    )

    history_columns = [
        column
        for column in featured.columns
        if column.startswith("traffic_volume_lag_")
        or column.startswith("traffic_volume_rolling_")
        or column.startswith("history_observed_ratio_")
    ]

    # Nhãn suy luận không được dùng để train. Các dòng đầu thiếu lịch sử đầy
    # đủ cũng bị loại để mọi hàng đầu ra có cùng cấu trúc feature.
    training_mask = (
        featured["target_observed"]
        & featured[history_columns].notna().all(axis=1)
    )
    feature_df = featured.loc[training_mask].copy()

    metadata_to_remove = [
        column
        for column in audit_df.columns
        if column != TIME_COLUMN
    ]
    feature_df = feature_df.drop(
        columns=metadata_to_remove,
        errors="ignore",
    )

    # Đặt target ở cuối giúp đọc CSV và tách X/y dễ hơn.
    feature_columns = [
        column
        for column in feature_df.columns
        if column not in [TIME_COLUMN, TARGET_COLUMN]
    ]
    feature_df = feature_df[
        [TIME_COLUMN] + feature_columns + [TARGET_COLUMN]
    ].reset_index(drop=True)

    report = build_feature_report(
        merged,
        feature_df,
        lag_hours,
        rolling_windows,
    )
    return feature_df, report


def save_time_series_features(
    feature_df,
    report,
    output_csv_path,
    output_report_path,
):
    """Lưu bảng feature và báo cáo bằng thao tác thay thế file an toàn."""
    for path in [output_csv_path, output_report_path]:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    temporary_csv_path = f"{output_csv_path}.tmp"
    feature_df.to_csv(temporary_csv_path, index=False)
    os.replace(temporary_csv_path, output_csv_path)

    temporary_report_path = f"{output_report_path}.tmp"
    with open(
        temporary_report_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(report, file, indent=4, ensure_ascii=False)
    os.replace(temporary_report_path, output_report_path)
