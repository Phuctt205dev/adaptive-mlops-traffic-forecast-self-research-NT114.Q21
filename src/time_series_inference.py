import json
import os
import time
from collections import OrderedDict
from datetime import datetime

import joblib
import numpy as np
import pandas as pd

from src.time_series_features import (
    DEFAULT_LAG_HOURS,
    DEFAULT_ROLLING_WINDOWS,
    add_calendar_features,
    merge_hourly_with_audit,
)
from src.time_series_preprocess import (
    ORIGINAL_COLUMN_ORDER,
    TARGET_COLUMN,
    TIME_COLUMN,
)
from src.time_series_training import calculate_regression_metrics


DEFAULT_MODEL_PATH = "models/time_series/best_time_series_model.pkl"
EXOGENOUS_COLUMNS = [
    column
    for column in ORIGINAL_COLUMN_ORDER
    if column not in [TIME_COLUMN, TARGET_COLUMN]
]


class InsufficientHistoryError(ValueError):
    """Báo rằng chưa có đủ lịch sử để tạo lag/rolling an toàn."""


def _as_boolean(value):
    """Đọc cờ bool ổn định từ bool, số hoặc chuỗi trong CSV."""
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "1.0"}
    return bool(value)


class TrafficHistory:
    """
    Bộ nhớ traffic chỉ chứa thông tin đã xuất hiện đến giờ hiện tại.

    Mỗi phần tử lưu target quan sát thật và causal target dùng cho lag. Khi
    cảm biến thiếu, causal target chỉ được suy ra từ các giờ trong quá khứ.
    """

    def __init__(self):
        self._entries = OrderedDict()

    def __len__(self):
        return len(self._entries)

    @property
    def last_timestamp(self):
        if not self._entries:
            return None
        return next(reversed(self._entries))

    def _entry_at(self, timestamp):
        return self._entries.get(pd.Timestamp(timestamp))

    def append(self, timestamp, traffic_volume, target_observed):
        """Thêm kết quả của một giờ sau khi dự đoán giờ đó đã hoàn tất."""
        timestamp = pd.Timestamp(timestamp)
        target_observed = _as_boolean(target_observed)

        if self.last_timestamp is not None:
            expected = self.last_timestamp + pd.Timedelta(hours=1)
            if timestamp != expected:
                raise ValueError(
                    "Lịch sử phải liên tục theo giờ. "
                    f"Mong đợi {expected}, nhận {timestamp}."
                )
        if timestamp in self._entries:
            raise ValueError(f"date_time bị trùng: {timestamp}")

        observed_value = (
            float(traffic_volume)
            if target_observed and pd.notna(traffic_volume)
            else None
        )
        causal_value, method = self._resolve_causal_value(
            timestamp,
            observed_value,
        )
        self._entries[timestamp] = {
            "observed_value": observed_value,
            "target_observed": target_observed,
            "causal_value": causal_value,
            "causal_method": method,
        }
        return causal_value, method

    def _resolve_causal_value(self, timestamp, observed_value):
        """Áp dụng đúng thứ tự fallback chỉ dựa trên lịch sử quá khứ."""
        if observed_value is not None:
            return observed_value, "observed"

        week_entry = self._entry_at(
            timestamp - pd.Timedelta(hours=168)
        )
        if week_entry and week_entry["target_observed"]:
            return (
                week_entry["observed_value"],
                "observed_168h_before",
            )

        day_entry = self._entry_at(
            timestamp - pd.Timedelta(hours=24)
        )
        if day_entry and day_entry["target_observed"]:
            return (
                day_entry["observed_value"],
                "observed_24h_before",
            )

        observed_values = [
            entry["observed_value"]
            for hours in range(1, 169)
            if (
                entry := self._entry_at(
                    timestamp - pd.Timedelta(hours=hours)
                )
            )
            and entry["target_observed"]
            and entry["observed_value"] is not None
        ]
        if observed_values:
            return (
                float(np.median(observed_values)),
                "past_168h_observed_median",
            )

        if self.last_timestamp is not None:
            return (
                self._entries[self.last_timestamp]["causal_value"],
                "previous_causal_value",
            )

        raise InsufficientHistoryError(
            "Không có target quan sát nào để khởi tạo lịch sử."
        )

    def _require_target_time(self, target_time):
        target_time = pd.Timestamp(target_time)
        if self.last_timestamp is None:
            raise InsufficientHistoryError("Lịch sử đang rỗng.")

        expected = self.last_timestamp + pd.Timedelta(hours=1)
        if target_time != expected:
            raise ValueError(
                "Chỉ được dự đoán giờ kế tiếp. "
                f"Mong đợi {expected}, nhận {target_time}."
            )
        if len(self) < max(
            max(DEFAULT_LAG_HOURS),
            max(DEFAULT_ROLLING_WINDOWS),
        ):
            raise InsufficientHistoryError(
                "Cần ít nhất 168 giờ lịch sử liên tục."
            )
        return target_time

    def build_history_features(self, target_time):
        """Tạo lag, rolling và cờ chất lượng cho đúng một giờ tương lai."""
        target_time = self._require_target_time(target_time)
        features = {}

        for hours in DEFAULT_LAG_HOURS:
            entry = self._entry_at(
                target_time - pd.Timedelta(hours=hours)
            )
            if entry is None:
                raise InsufficientHistoryError(
                    f"Thiếu lịch sử tại lag {hours} giờ."
                )
            features[f"traffic_volume_lag_{hours}h"] = (
                entry["causal_value"]
            )
            features[f"lag_{hours}h_target_observed"] = int(
                entry["target_observed"]
            )

        for window in DEFAULT_ROLLING_WINDOWS:
            window_entries = [
                self._entry_at(
                    target_time - pd.Timedelta(hours=hours)
                )
                for hours in range(window, 0, -1)
            ]
            if any(entry is None for entry in window_entries):
                raise InsufficientHistoryError(
                    f"Thiếu dữ liệu cho rolling {window} giờ."
                )

            values = np.asarray(
                [
                    entry["causal_value"]
                    for entry in window_entries
                ],
                dtype=float,
            )
            prefix = f"traffic_volume_rolling_{window}h"
            features[f"{prefix}_mean"] = float(values.mean())
            features[f"{prefix}_median"] = float(
                np.median(values)
            )
            features[f"{prefix}_std"] = float(values.std(ddof=0))
            features[f"{prefix}_min"] = float(values.min())
            features[f"{prefix}_max"] = float(values.max())
            features[f"history_observed_ratio_{window}h"] = float(
                np.mean(
                    [
                        entry["target_observed"]
                        for entry in window_entries
                    ]
                )
            )
        return features

    @classmethod
    def from_dataframe(cls, dataframe):
        """Khởi tạo lịch sử từ các giờ đã xảy ra, theo đúng thứ tự."""
        required_columns = {
            TIME_COLUMN,
            TARGET_COLUMN,
            "target_observed",
        }
        missing = sorted(required_columns - set(dataframe.columns))
        if missing:
            raise ValueError(
                "Dữ liệu lịch sử thiếu các cột: "
                + ", ".join(missing)
            )

        ordered = dataframe.copy()
        ordered[TIME_COLUMN] = pd.to_datetime(
            ordered[TIME_COLUMN],
            errors="raise",
        )
        ordered = ordered.sort_values(TIME_COLUMN)

        history = cls()
        for row in ordered.itertuples(index=False):
            history.append(
                getattr(row, TIME_COLUMN),
                getattr(row, TARGET_COLUMN),
                getattr(row, "target_observed"),
            )
        return history


def build_next_hour_feature_row(
    target_time,
    exogenous_features,
    history,
    expected_feature_columns=None,
):
    """Ghép thời tiết hiện tại với lag/rolling lấy từ TrafficHistory."""
    missing_columns = [
        column
        for column in EXOGENOUS_COLUMNS
        if column not in exogenous_features
    ]
    if missing_columns:
        raise ValueError(
            "Thiếu feature ngoại sinh: "
            + ", ".join(missing_columns)
        )

    row = {
        TIME_COLUMN: pd.Timestamp(target_time),
        **{
            column: exogenous_features[column]
            for column in EXOGENOUS_COLUMNS
        },
    }
    calendar_df = add_calendar_features(pd.DataFrame([row]))
    feature_row = calendar_df.drop(columns=[TIME_COLUMN])

    history_features = history.build_history_features(target_time)
    for column, value in history_features.items():
        feature_row[column] = value

    if expected_feature_columns is not None:
        expected = list(expected_feature_columns)
        missing_expected = [
            column
            for column in expected
            if column not in feature_row.columns
        ]
        unexpected = [
            column
            for column in feature_row.columns
            if column not in expected
        ]
        if missing_expected or unexpected:
            raise ValueError(
                "Feature inference không khớp model. "
                f"Thiếu={missing_expected}, dư={unexpected}"
            )
        feature_row = feature_row[expected]
    return feature_row


def load_time_series_model(model_path=DEFAULT_MODEL_PATH):
    """Load model Giai đoạn 3 và kiểm tra hợp đồng feature."""
    if not os.path.exists(model_path):
        raise FileNotFoundError(
            f"Không tìm thấy model time series: {model_path}"
        )
    model = joblib.load(model_path)
    if not hasattr(model, "feature_names_in_"):
        raise ValueError("Model không lưu feature_names_in_.")
    return model


def predict_next_hour(
    model,
    target_time,
    exogenous_features,
    history,
):
    """Dự đoán một giờ kế tiếp từ lịch sử đã biết."""
    feature_row = build_next_hour_feature_row(
        target_time,
        exogenous_features,
        history,
        expected_feature_columns=model.feature_names_in_,
    )
    prediction = float(model.predict(feature_row)[0])
    return prediction, feature_row


def _save_json(data, path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)
    os.replace(temporary_path, path)


def run_sequential_backtest(
    model,
    hourly_df,
    audit_df,
    start_time,
    end_time,
    predictions_path="results/time_series_sequential_backtest.csv",
    report_path="results/time_series_sequential_backtest.json",
    phase3_report=None,
):
    """
    Mô phỏng production: dự đoán trước, sau đó mới nhận target của giờ đó.

    Mọi giờ đều được đưa vào lịch sử. Chỉ giờ có target thật mới dùng tính
    metric, đúng với chính sách nhãn của Giai đoạn 2.
    """
    merged = merge_hourly_with_audit(hourly_df, audit_df)
    start_time = pd.Timestamp(start_time)
    end_time = pd.Timestamp(end_time)
    if start_time > end_time:
        raise ValueError("start_time phải nhỏ hơn hoặc bằng end_time.")

    history_rows = merged[merged[TIME_COLUMN] < start_time]
    backtest_rows = merged[
        (merged[TIME_COLUMN] >= start_time)
        & (merged[TIME_COLUMN] <= end_time)
    ]
    if history_rows.empty or backtest_rows.empty:
        raise ValueError("Không đủ dữ liệu cho khoảng backtest.")

    history = TrafficHistory.from_dataframe(history_rows)
    results = []
    skipped_evaluation_rows = 0
    prediction_seconds = 0.0

    for row in backtest_rows.to_dict(orient="records"):
        timestamp = row[TIME_COLUMN]
        exogenous = {
            column: row[column]
            for column in EXOGENOUS_COLUMNS
        }

        started = time.perf_counter()
        prediction, feature_row = predict_next_hour(
            model,
            timestamp,
            exogenous,
            history,
        )
        prediction_seconds += time.perf_counter() - started

        target_observed = _as_boolean(row["target_observed"])
        if target_observed:
            actual = float(row[TARGET_COLUMN])
            results.append(
                {
                    TIME_COLUMN: timestamp,
                    "actual": actual,
                    "prediction": prediction,
                    "absolute_error": abs(actual - prediction),
                    "lag_1h": float(
                        feature_row.iloc[0][
                            "traffic_volume_lag_1h"
                        ]
                    ),
                    "lag_24h": float(
                        feature_row.iloc[0][
                            "traffic_volume_lag_24h"
                        ]
                    ),
                    "lag_168h": float(
                        feature_row.iloc[0][
                            "traffic_volume_lag_168h"
                        ]
                    ),
                }
            )
        else:
            skipped_evaluation_rows += 1

        # Target của timestamp chỉ được thêm sau khi prediction đã hoàn tất.
        history.append(
            timestamp,
            row[TARGET_COLUMN],
            target_observed,
        )

    result_df = pd.DataFrame(results)
    if result_df.empty:
        raise ValueError("Không có target quan sát để đánh giá.")

    output_directory = os.path.dirname(predictions_path)
    if output_directory:
        os.makedirs(output_directory, exist_ok=True)
    temporary_predictions = f"{predictions_path}.tmp"
    result_df.to_csv(temporary_predictions, index=False)
    os.replace(temporary_predictions, predictions_path)

    metrics = calculate_regression_metrics(
        result_df["actual"],
        result_df["prediction"],
    )
    phase3_metrics = None
    metric_delta = None
    if phase3_report and "test_metrics" in phase3_report:
        phase3_metrics = phase3_report["test_metrics"]
        metric_delta = {
            metric: round(
                metrics[metric] - float(phase3_metrics[metric]),
                4,
            )
            for metric in ["MAE", "RMSE", "MAPE", "WAPE"]
        }

    report = {
        "created_at": datetime.now().isoformat(),
        "mode": "sequential_one_step_ahead",
        "prediction_before_history_update": True,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "calendar_hours_processed": int(len(backtest_rows)),
        "evaluated_observed_targets": int(len(result_df)),
        "skipped_unobserved_targets": int(
            skipped_evaluation_rows
        ),
        "metrics": metrics,
        "phase3_batch_metrics": phase3_metrics,
        "metric_delta_vs_phase3": metric_delta,
        "prediction_seconds": round(prediction_seconds, 4),
        "prediction_ms_per_calendar_hour": round(
            prediction_seconds * 1000 / len(backtest_rows),
            6,
        ),
        "predictions_path": predictions_path,
        "history_policy": (
            "Observed target; fallback 168h, 24h, past median, "
            "then previous causal value"
        ),
        "production_status": (
            "experimental_not_connected_to_champion_or_api"
        ),
    }
    _save_json(report, report_path)
    return report, result_df
