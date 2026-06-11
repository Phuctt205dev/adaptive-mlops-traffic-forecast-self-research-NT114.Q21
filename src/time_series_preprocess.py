import json
import os
from datetime import datetime

import numpy as np
import pandas as pd


TIME_COLUMN = "date_time"
TARGET_COLUMN = "traffic_volume"

NUMERIC_FEATURE_COLUMNS = [
    "air_pollution_index",
    "humidity",
    "wind_speed",
    "wind_direction",
    "visibility_in_miles",
    "dew_point",
    "temperature",
    "rain_p_h",
    "snow_p_h",
    "clouds_all",
]

CATEGORICAL_COLUMNS = [
    "weather_type",
    "weather_description",
]

ORIGINAL_COLUMN_ORDER = [
    TIME_COLUMN,
    "is_holiday",
    *NUMERIC_FEATURE_COLUMNS,
    *CATEGORICAL_COLUMNS,
    TARGET_COLUMN,
]

METADATA_COLUMNS = [
    "source_row_count",
    "is_observed_hour",
    "feature_is_imputed",
    "feature_imputation_method",
    "target_observed",
    "target_is_imputed",
    "target_imputation_method",
    "imputation_confidence",
    "gap_length_hours",
]

REQUIRED_COLUMNS = ORIGINAL_COLUMN_ORDER

# Làm tròn theo cách dữ liệu gốc thường được ghi.
ROUNDING_RULES = {
    "air_pollution_index": 0,
    "humidity": 0,
    "wind_speed": 0,
    "wind_direction": 0,
    "visibility_in_miles": 0,
    "dew_point": 0,
    "temperature": 2,
    "rain_p_h": 2,
    "snow_p_h": 2,
    "clouds_all": 0,
    TARGET_COLUMN: 0,
}

INTEGER_OUTPUT_COLUMNS = [
    "air_pollution_index",
    "humidity",
    "wind_speed",
    "wind_direction",
    "visibility_in_miles",
    "dew_point",
    "clouds_all",
    TARGET_COLUMN,
    "source_row_count",
    "gap_length_hours",
]

SHORT_GAP_HOURS = 6
SEASONAL_LOOKAROUND_DAYS = 35


def validate_input_columns(df):
    """Kiểm tra file đầu vào có đủ các cột bắt buộc hay không."""
    missing_columns = [
        column
        for column in REQUIRED_COLUMNS
        if column not in df.columns
    ]

    if missing_columns:
        raise ValueError(
            "Dữ liệu thiếu các cột bắt buộc: "
            + ", ".join(missing_columns)
        )


def most_common_value(series):
    """Lấy giá trị xuất hiện nhiều nhất trong một nhóm."""
    values = series.dropna()
    if values.empty:
        return pd.NA
    return values.mode().iloc[0]


def aggregate_duplicate_hours(df):
    """
    Gộp các dòng cùng giờ thành một dòng.

    Các feature số lấy trung bình, thời tiết dạng chữ lấy mode và target lấy
    trung bình. CSV hiện tại không có target mâu thuẫn trong cùng một giờ.
    """
    aggregations = {
        column: "mean"
        for column in NUMERIC_FEATURE_COLUMNS
    }
    aggregations.update(
        {
            # Giữ tên ngày lễ như CSV gốc, ví dụ "Christmas Day".
            "is_holiday": most_common_value,
            "weather_type": most_common_value,
            "weather_description": most_common_value,
            TARGET_COLUMN: "mean",
        }
    )

    source_counts = (
        df.groupby(TIME_COLUMN)
        .size()
        .rename("source_row_count")
    )
    hourly = (
        df.groupby(TIME_COLUMN, as_index=True)
        .agg(aggregations)
        .sort_index()
        .join(source_counts)
    )
    hourly["is_observed_hour"] = True
    hourly["target_observed"] = hourly[TARGET_COLUMN].notna()
    return hourly


def insert_missing_hours(hourly_df):
    """Chèn các giờ bị thiếu và tính độ dài đoạn thiếu chứa từng giờ."""
    complete_index = pd.date_range(
        start=hourly_df.index.min(),
        end=hourly_df.index.max(),
        freq="h",
        name=TIME_COLUMN,
    )
    complete = hourly_df.reindex(complete_index)
    complete["is_observed_hour"] = complete[
        "is_observed_hour"
    ].eq(True)
    complete["target_observed"] = complete[
        "target_observed"
    ].eq(True)
    complete["source_row_count"] = (
        complete["source_row_count"]
        .fillna(0)
        .astype(int)
    )

    missing_mask = ~complete["is_observed_hour"]
    group_ids = missing_mask.ne(missing_mask.shift()).cumsum()
    gap_lengths = (
        missing_mask[missing_mask]
        .groupby(group_ids[missing_mask])
        .transform("size")
    )
    complete["gap_length_hours"] = 0
    complete.loc[missing_mask, "gap_length_hours"] = gap_lengths
    complete["gap_length_hours"] = complete[
        "gap_length_hours"
    ].astype(int)
    return complete


def _weighted_average(values, distances):
    """Tính trung bình có trọng số, mốc gần hơn có trọng số lớn hơn."""
    weights = 1.0 / (distances.astype(float) + 1.0)
    return float(np.average(values.astype(float), weights=weights))


def _build_seasonal_candidate_cache(observed_df):
    """
    Lập chỉ mục theo giờ và thứ để không quét toàn bộ dữ liệu cho mỗi ô thiếu.

    Hai nhóm được giữ riêng vì thuật toán ưu tiên cùng giờ + cùng thứ, sau đó
    mới fallback sang chỉ cùng giờ.
    """
    by_hour = {
        int(hour): group.sort_index()
        for hour, group in observed_df.groupby(
            observed_df.index.hour,
            sort=False,
        )
    }
    by_hour_weekday = {
        (int(hour), int(weekday)): group.sort_index()
        for (hour, weekday), group in observed_df.groupby(
            [
                observed_df.index.hour,
                observed_df.index.dayofweek,
            ],
            sort=False,
        )
    }
    by_month_hour = {
        (int(month), int(hour)): group.sort_index()
        for (month, hour), group in observed_df.groupby(
            [
                observed_df.index.month,
                observed_df.index.hour,
            ],
            sort=False,
        )
    }
    by_month_hour_weekday = {
        (int(month), int(hour), int(weekday)): group.sort_index()
        for (month, hour, weekday), group in observed_df.groupby(
            [
                observed_df.index.month,
                observed_df.index.hour,
                observed_df.index.dayofweek,
            ],
            sort=False,
        )
    }
    return {
        "by_hour": by_hour,
        "by_hour_weekday": by_hour_weekday,
        "by_month_hour": by_month_hour,
        "by_month_hour_weekday": by_month_hour_weekday,
    }


def _seasonal_candidates(
    observed_df,
    timestamp,
    require_same_weekday=True,
    candidate_cache=None,
):
    """
    Tìm dữ liệu ở cùng giờ trong các ngày hoặc tuần lân cận.

    Cùng thứ được ưu tiên vì giao thông thứ Hai thường khác Chủ nhật.
    """
    if candidate_cache is None:
        source = observed_df
        hour_mask = source.index.hour == timestamp.hour
        if require_same_weekday:
            hour_mask &= (
                source.index.dayofweek == timestamp.dayofweek
            )
        source = source.loc[hour_mask]
    elif require_same_weekday:
        source = candidate_cache["by_hour_weekday"].get(
            (int(timestamp.hour), int(timestamp.dayofweek)),
        )
    else:
        source = candidate_cache["by_hour"].get(int(timestamp.hour))

    if source is None or source.empty:
        return observed_df.iloc[0:0].copy()

    start = timestamp - pd.Timedelta(
        days=SEASONAL_LOOKAROUND_DAYS
    )
    end = timestamp + pd.Timedelta(
        days=SEASONAL_LOOKAROUND_DAYS
    )
    candidates = source.loc[start:end].copy()
    if candidates.empty:
        return candidates

    candidates["_day_distance"] = np.abs(
        (
            candidates.index.normalize()
            - timestamp.normalize()
        ).days
    )
    return candidates


def _climatology_candidates(
    observed_df,
    timestamp,
    require_same_weekday=True,
    candidate_cache=None,
):
    """Lấy mẫu cùng tháng/giờ ở các năm khác cho khoảng trống rất dài."""
    if candidate_cache is None:
        mask = (
            (observed_df.index.month == timestamp.month)
            & (observed_df.index.hour == timestamp.hour)
        )
        if require_same_weekday:
            mask &= (
                observed_df.index.dayofweek == timestamp.dayofweek
            )
        candidates = observed_df.loc[mask].copy()
    elif require_same_weekday:
        candidates = candidate_cache[
            "by_month_hour_weekday"
        ].get(
            (
                int(timestamp.month),
                int(timestamp.hour),
                int(timestamp.dayofweek),
            )
        )
    else:
        candidates = candidate_cache["by_month_hour"].get(
            (int(timestamp.month), int(timestamp.hour))
        )

    if candidates is None or candidates.empty:
        return observed_df.iloc[0:0].copy()

    candidates = candidates.copy()
    candidates["_day_distance"] = np.abs(
        (
            candidates.index.normalize()
            - timestamp.normalize()
        ).days
    )
    return candidates


def _estimate_numeric_from_seasonality(
    observed_df,
    timestamp,
    column,
    candidate_cache=None,
):
    """Ước lượng feature số bằng cùng giờ, ưu tiên cùng thứ."""
    candidates = _seasonal_candidates(
        observed_df,
        timestamp,
        require_same_weekday=True,
        candidate_cache=candidate_cache,
    )
    candidates = candidates.dropna(subset=[column])
    method = "seasonal_same_hour_weekday"

    if len(candidates) < 2:
        candidates = _seasonal_candidates(
            observed_df,
            timestamp,
            require_same_weekday=False,
            candidate_cache=candidate_cache,
        ).dropna(subset=[column])
        method = "seasonal_same_hour"

    if candidates.empty:
        candidates = _climatology_candidates(
            observed_df,
            timestamp,
            require_same_weekday=True,
            candidate_cache=candidate_cache,
        ).dropna(subset=[column])
        method = "climatology_month_hour_weekday"

    if candidates.empty:
        candidates = _climatology_candidates(
            observed_df,
            timestamp,
            require_same_weekday=False,
            candidate_cache=candidate_cache,
        ).dropna(subset=[column])
        method = "climatology_month_hour"

    if candidates.empty:
        return np.nan, "unavailable", 0.0

    value = _weighted_average(
        candidates[column],
        candidates["_day_distance"],
    )
    confidence_cap = (
        0.6 if method.startswith("climatology_") else 0.85
    )
    confidence = min(
        confidence_cap,
        0.35 + len(candidates) * 0.05,
    )
    return value, method, confidence


def _estimate_target(
    completed_df,
    observed_df,
    timestamp,
    candidate_cache=None,
):
    """
    Ước lượng traffic theo nhịp ngày/tuần.

    Ưu tiên dữ liệu cùng giờ, cùng thứ ở các tuần lân cận. Nếu không đủ dữ liệu,
    khoảng thiếu ngắn mới dùng nội suy giữa hai traffic quan sát gần nhất.
    """
    candidates = _seasonal_candidates(
        observed_df,
        timestamp,
        require_same_weekday=True,
        candidate_cache=candidate_cache,
    ).dropna(subset=[TARGET_COLUMN])

    if len(candidates) >= 2:
        value = _weighted_average(
            candidates[TARGET_COLUMN],
            candidates["_day_distance"],
        )
        confidence = min(0.9, 0.55 + len(candidates) * 0.05)
        return (
            value,
            "seasonal_same_hour_weekday",
            confidence,
        )

    candidates = _seasonal_candidates(
        observed_df,
        timestamp,
        require_same_weekday=False,
        candidate_cache=candidate_cache,
    ).dropna(subset=[TARGET_COLUMN])

    if len(candidates) >= 2:
        value = _weighted_average(
            candidates[TARGET_COLUMN],
            candidates["_day_distance"],
        )
        confidence = min(0.75, 0.4 + len(candidates) * 0.04)
        return value, "seasonal_same_hour", confidence

    candidates = _climatology_candidates(
        observed_df,
        timestamp,
        require_same_weekday=True,
        candidate_cache=candidate_cache,
    ).dropna(subset=[TARGET_COLUMN])

    if len(candidates) >= 2:
        value = _weighted_average(
            candidates[TARGET_COLUMN],
            candidates["_day_distance"],
        )
        confidence = min(0.6, 0.3 + len(candidates) * 0.03)
        return (
            value,
            "climatology_month_hour_weekday",
            confidence,
        )

    candidates = _climatology_candidates(
        observed_df,
        timestamp,
        require_same_weekday=False,
        candidate_cache=candidate_cache,
    ).dropna(subset=[TARGET_COLUMN])

    if len(candidates) >= 2:
        value = _weighted_average(
            candidates[TARGET_COLUMN],
            candidates["_day_distance"],
        )
        confidence = min(0.5, 0.25 + len(candidates) * 0.02)
        return value, "climatology_month_hour", confidence

    gap_length = int(
        completed_df.at[timestamp, "gap_length_hours"]
    )
    if gap_length <= SHORT_GAP_HOURS:
        before = observed_df.loc[
            observed_df.index < timestamp,
            TARGET_COLUMN,
        ].dropna()
        after = observed_df.loc[
            observed_df.index > timestamp,
            TARGET_COLUMN,
        ].dropna()

        if not before.empty and not after.empty:
            before_time = before.index[-1]
            after_time = after.index[0]
            total_seconds = (
                after_time - before_time
            ).total_seconds()
            elapsed_seconds = (
                timestamp - before_time
            ).total_seconds()
            ratio = elapsed_seconds / total_seconds
            value = (
                before.iloc[-1]
                + ratio * (after.iloc[0] - before.iloc[-1])
            )
            return value, "short_gap_linear", 0.45

    return np.nan, "unavailable", 0.0


def _estimate_weather_pair(
    observed_df,
    timestamp,
    candidate_cache=None,
):
    """Ước lượng weather type và description theo cùng giờ lân cận."""
    candidates = _seasonal_candidates(
        observed_df,
        timestamp,
        require_same_weekday=True,
        candidate_cache=candidate_cache,
    )
    method = "seasonal_same_hour_weekday"

    if len(candidates) < 2:
        candidates = _seasonal_candidates(
            observed_df,
            timestamp,
            require_same_weekday=False,
            candidate_cache=candidate_cache,
        )
        method = "seasonal_same_hour"

    if candidates.empty:
        candidates = _climatology_candidates(
            observed_df,
            timestamp,
            require_same_weekday=True,
            candidate_cache=candidate_cache,
        )
        method = "climatology_month_hour_weekday"

    if candidates.empty:
        candidates = _climatology_candidates(
            observed_df,
            timestamp,
            require_same_weekday=False,
            candidate_cache=candidate_cache,
        )
        method = "climatology_month_hour"

    candidates = candidates.dropna(
        subset=CATEGORICAL_COLUMNS
    )
    if candidates.empty:
        return "Unknown", "Unknown", "unavailable", 0.0

    pairs = candidates[
        CATEGORICAL_COLUMNS
    ].astype(str).agg(" | ".join, axis=1)
    selected_pair = pairs.mode().iloc[0]
    weather_type, description = selected_pair.split(
        " | ",
        maxsplit=1,
    )
    confidence_cap = (
        0.55 if method.startswith("climatology_") else 0.8
    )
    confidence = min(
        confidence_cap,
        0.3 + len(candidates) * 0.05,
    )
    return weather_type, description, method, confidence


def _fill_holiday_from_observed_dates(completed_df):
    """Dùng trạng thái ngày lễ của các giờ quan sát trong cùng ngày."""
    result = completed_df.copy()
    observed = result[result["is_observed_hour"]]
    holiday_by_date = observed.groupby(
        observed.index.date
    )["is_holiday"].agg(most_common_value)

    missing_mask = ~result["is_observed_hour"]
    missing_dates = pd.Series(
        result.index.date,
        index=result.index,
    )
    inferred = missing_dates.map(holiday_by_date)
    result.loc[missing_mask, "is_holiday"] = inferred[missing_mask]
    return result


def impute_missing_hours(hourly_df):
    """
    Ước lượng các giờ thiếu bằng ngữ cảnh thời gian xung quanh.

    - Đoạn ngắn: feature số có thể nội suy tuyến tính.
    - Đoạn dài: dùng cùng giờ và cùng thứ ở các tuần lân cận.
    - Target được điền để chuỗi dễ sử dụng, nhưng luôn có target_observed=False.
    """
    result = _fill_holiday_from_observed_dates(hourly_df)
    observed_df = result[result["is_observed_hour"]].copy()
    candidate_cache = _build_seasonal_candidate_cache(observed_df)
    missing_index = result.index[~result["is_observed_hour"]]

    result["feature_is_imputed"] = ~result["is_observed_hour"]
    result["feature_imputation_method"] = "observed"
    result["target_is_imputed"] = False
    result["target_imputation_method"] = "observed"
    result["imputation_confidence"] = 1.0

    # Nội suy tuyến tính chỉ được tạo cho đoạn ngắn. Đoạn dài không dùng các số
    # nằm giữa hai mốc cách nhau nhiều ngày vì chúng tạo cảm giác chính xác giả.
    short_gap_mask = (
        ~result["is_observed_hour"]
        & (result["gap_length_hours"] <= SHORT_GAP_HOURS)
    )
    short_linear = result[NUMERIC_FEATURE_COLUMNS].interpolate(
        method="time",
        limit=SHORT_GAP_HOURS,
        limit_area="inside",
    )

    for timestamp in missing_index:
        methods = []
        confidences = []
        is_short_gap = bool(short_gap_mask.loc[timestamp])

        for column in NUMERIC_FEATURE_COLUMNS:
            if is_short_gap and pd.notna(
                short_linear.at[timestamp, column]
            ):
                value = short_linear.at[timestamp, column]
                method = "short_gap_linear"
                confidence = 0.9
            else:
                value, method, confidence = (
                    _estimate_numeric_from_seasonality(
                        observed_df,
                        timestamp,
                        column,
                        candidate_cache=candidate_cache,
                    )
                )

            result.at[timestamp, column] = value
            methods.append(method)
            confidences.append(confidence)

        (
            weather_type,
            weather_description,
            weather_method,
            weather_confidence,
        ) = _estimate_weather_pair(
            observed_df,
            timestamp,
            candidate_cache=candidate_cache,
        )
        result.at[timestamp, "weather_type"] = weather_type
        result.at[
            timestamp,
            "weather_description",
        ] = weather_description
        methods.append(weather_method)
        confidences.append(weather_confidence)

        (
            target_value,
            target_method,
            target_confidence,
        ) = _estimate_target(
            result,
            observed_df,
            timestamp,
            candidate_cache=candidate_cache,
        )
        result.at[timestamp, TARGET_COLUMN] = target_value
        result.at[timestamp, "target_is_imputed"] = pd.notna(
            target_value
        )
        result.at[
            timestamp,
            "target_imputation_method",
        ] = target_method
        confidences.append(target_confidence)

        unique_methods = sorted(set(methods))
        result.at[
            timestamp,
            "feature_imputation_method",
        ] = "+".join(unique_methods)
        result.at[timestamp, "imputation_confidence"] = round(
            float(np.mean(confidences)),
            3,
        )

    return result


def round_to_source_format(hourly_df):
    """
    Làm tròn để dữ liệu giống định dạng CSV gốc hơn.

    Các cột vốn là số đếm/chỉ số nguyên được làm tròn 0 chữ số. Nhiệt độ, mưa
    và tuyết giữ tối đa 2 chữ số thập phân.
    """
    result = hourly_df.copy()
    for column, decimals in ROUNDING_RULES.items():
        result[column] = result[column].round(decimals)

    # Nullable Int64 vẫn cho phép giữ pd.NA nếu một giá trị không thể suy luận.
    for column in INTEGER_OUTPUT_COLUMNS:
        result[column] = result[column].astype("Int64")

    return result


def evaluate_target_imputation_quality(
    completed_df,
    sample_size=500,
):
    """
    Che một số target thật rồi đoán lại để kiểm tra chất lượng suy luận.

    Đây là backtest offline, không phải điểm của model forecasting. Nó chỉ cho
    biết quy tắc điền target có bám dữ liệu thật ở mức nào.
    """
    observed_df = completed_df[
        completed_df["target_observed"]
    ].copy()
    safe_start = (
        observed_df.index.min()
        + pd.Timedelta(days=SEASONAL_LOOKAROUND_DAYS)
    )
    safe_end = (
        observed_df.index.max()
        - pd.Timedelta(days=SEASONAL_LOOKAROUND_DAYS)
    )
    eligible = observed_df[
        (observed_df.index >= safe_start)
        & (observed_df.index <= safe_end)
    ]

    if eligible.empty:
        return {
            "sample_size": 0,
            "status": "insufficient_data",
        }

    actual_sample_size = min(sample_size, len(eligible))
    positions = np.linspace(
        0,
        len(eligible) - 1,
        actual_sample_size,
        dtype=int,
    )
    sample = eligible.iloc[positions]

    absolute_errors = []
    percentage_errors = []
    method_counts = {}

    for timestamp, row in sample.iterrows():
        # Bỏ đúng thời điểm đang kiểm tra để thuật toán không nhìn đáp án.
        observed_without_current = observed_df.drop(
            index=timestamp
        )
        prediction, method, _ = _estimate_target(
            completed_df,
            observed_without_current,
            timestamp,
        )
        if pd.isna(prediction):
            continue

        actual = float(row[TARGET_COLUMN])
        error = abs(actual - float(prediction))
        absolute_errors.append(error)
        if actual != 0:
            percentage_errors.append(error / abs(actual) * 100)
        method_counts[method] = method_counts.get(method, 0) + 1

    if not absolute_errors:
        return {
            "sample_size": 0,
            "status": "no_predictions",
        }

    return {
        "sample_size": len(absolute_errors),
        "status": "ok",
        "MAE": round(float(np.mean(absolute_errors)), 4),
        "median_absolute_error": round(
            float(np.median(absolute_errors)),
            4,
        ),
        "p90_absolute_error": round(
            float(np.percentile(absolute_errors, 90)),
            4,
        ),
        "MAPE": round(float(np.mean(percentage_errors)), 4),
        "method_counts": method_counts,
        "note": (
            "Backtest offline bằng cách che target thật; "
            "không phải metric của model forecasting"
        ),
    }


def build_quality_report(
    raw_df,
    parsed_df,
    aggregated_df,
    completed_df,
):
    """Tạo báo cáo minh bạch về dữ liệu quan sát và dữ liệu suy luận."""
    duplicate_mask = parsed_df.duplicated(
        subset=[TIME_COLUMN],
        keep=False,
    )
    duplicate_rows = parsed_df[duplicate_mask]

    if duplicate_rows.empty:
        target_conflict_hours = 0
    else:
        target_conflict_hours = int(
            (
                duplicate_rows
                .groupby(TIME_COLUMN)[TARGET_COLUMN]
                .nunique()
                > 1
            ).sum()
        )

    synthetic_rows = completed_df[
        ~completed_df["is_observed_hour"]
    ]
    method_counts = (
        synthetic_rows["target_imputation_method"]
        .value_counts(dropna=False)
        .to_dict()
    )
    method_counts = {
        str(key): int(value)
        for key, value in method_counts.items()
    }
    imputation_backtest = evaluate_target_imputation_quality(
        completed_df
    )

    return {
        "created_at": datetime.now().isoformat(),
        "source_rows": int(len(raw_df)),
        "source_start": parsed_df[TIME_COLUMN].min().isoformat(),
        "source_end": parsed_df[TIME_COLUMN].max().isoformat(),
        "unique_source_hours": int(parsed_df[TIME_COLUMN].nunique()),
        "duplicate_extra_rows": int(
            parsed_df.duplicated(TIME_COLUMN).sum()
        ),
        "duplicate_hour_groups": int(
            duplicate_rows[TIME_COLUMN].nunique()
        ),
        "duplicate_hours_with_target_conflict": (
            target_conflict_hours
        ),
        "rows_after_duplicate_aggregation": int(
            len(aggregated_df)
        ),
        "rows_after_hourly_reindex": int(len(completed_df)),
        "inserted_missing_hours": int(len(synthetic_rows)),
        "observed_target_rows": int(
            completed_df["target_observed"].sum()
        ),
        "imputed_target_rows": int(
            completed_df["target_is_imputed"].sum()
        ),
        "unavailable_target_rows": int(
            completed_df[TARGET_COLUMN].isna().sum()
        ),
        "maximum_gap_hours": int(
            completed_df["gap_length_hours"].max()
        ),
        "short_gap_threshold_hours": SHORT_GAP_HOURS,
        "target_imputation_methods": method_counts,
        "target_imputation_backtest": imputation_backtest,
        "frequency": "1h",
        "training_label_policy": (
            "Chỉ dùng target_observed=True làm nhãn huấn luyện"
        ),
        "numeric_feature_policy": (
            "Đoạn <=6 giờ nội suy tuyến tính; đoạn dài dùng cùng giờ/cùng "
            "thứ; khoảng rất dài fallback cùng tháng/giờ ở các năm khác"
        ),
        "target_policy": (
            "Ưu tiên cùng giờ/cùng thứ trong +/-35 ngày; khoảng rất dài "
            "fallback cùng tháng/giờ ở các năm khác"
        ),
        "rounding_policy": ROUNDING_RULES,
    }


def prepare_hourly_time_series(raw_df):
    """
    Chuẩn hóa dữ liệu thành chuỗi mỗi giờ có nguồn gốc minh bạch.

    Dữ liệu suy luận được điền để giữ định dạng liên tục, nhưng các cờ metadata
    cho phép loại chúng khỏi nhãn huấn luyện.
    """
    validate_input_columns(raw_df)

    parsed_df = raw_df.copy()
    parsed_df[TIME_COLUMN] = pd.to_datetime(
        parsed_df[TIME_COLUMN],
        errors="raise",
    )
    parsed_df = parsed_df.sort_values(TIME_COLUMN)
    aggregated_df = aggregate_duplicate_hours(parsed_df)
    completed_df = insert_missing_hours(aggregated_df)
    completed_df = impute_missing_hours(completed_df)
    completed_df = round_to_source_format(completed_df)

    report = build_quality_report(
        raw_df=raw_df,
        parsed_df=parsed_df,
        aggregated_df=aggregated_df,
        completed_df=completed_df,
    )

    completed_df = completed_df.reset_index()

    # CSV chính giữ đúng schema của file gốc.
    hourly_df = completed_df[ORIGINAL_COLUMN_ORDER].copy()

    # Metadata nằm ở file riêng và có thể nối lại bằng date_time.
    audit_df = completed_df[
        [TIME_COLUMN] + METADATA_COLUMNS
    ].copy()
    return hourly_df, audit_df, report


def save_hourly_dataset(
    hourly_df,
    audit_df,
    report,
    output_csv_path,
    output_audit_path,
    output_report_path,
):
    """Lưu CSV đã chuẩn hóa và báo cáo JSON."""
    csv_directory = os.path.dirname(output_csv_path)
    audit_directory = os.path.dirname(output_audit_path)
    report_directory = os.path.dirname(output_report_path)

    if csv_directory:
        os.makedirs(csv_directory, exist_ok=True)
    if audit_directory:
        os.makedirs(audit_directory, exist_ok=True)
    if report_directory:
        os.makedirs(report_directory, exist_ok=True)

    # "None" giữ cách biểu diễn ngày thường giống CSV nguồn.
    hourly_df.to_csv(output_csv_path, index=False, na_rep="None")
    audit_df.to_csv(output_audit_path, index=False)

    temporary_report_path = f"{output_report_path}.tmp"
    with open(
        temporary_report_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            report,
            file,
            indent=4,
            ensure_ascii=False,
        )
    os.replace(temporary_report_path, output_report_path)
