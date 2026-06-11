import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit

from src.time_series_preprocess import TIME_COLUMN


# Tất cả mốc trước PRODUCTION_START được phép dùng cho nghiên cứu offline.
PRODUCTION_START = pd.Timestamp("2016-01-01 00:00:00")
FINAL_TEST_START = pd.Timestamp("2015-10-01 00:00:00")
OFFLINE_END = PRODUCTION_START - pd.Timedelta(hours=1)
DEVELOPMENT_END = FINAL_TEST_START - pd.Timedelta(hours=1)
DEFAULT_CV_SPLITS = 5


def _prepare_timestamps(dataframe):
    """Parse và sắp xếp timestamp trước khi chia dữ liệu."""
    if TIME_COLUMN not in dataframe.columns:
        raise ValueError(f"Dữ liệu thiếu cột {TIME_COLUMN}.")

    result = dataframe.copy()
    result[TIME_COLUMN] = pd.to_datetime(
        result[TIME_COLUMN],
        errors="raise",
    )
    result = result.sort_values(TIME_COLUMN).reset_index(drop=True)
    if result[TIME_COLUMN].duplicated().any():
        raise ValueError("Dữ liệu có date_time bị trùng.")
    return result


def split_offline_and_production(dataframe):
    """
    Khóa production từ năm 2016 để không bị dùng khi chọn model.

    Offline chỉ kéo dài đến hết năm 2015. Production được trả về riêng nhằm
    phục vụ mô phỏng drift ở giai đoạn sau, không tham gia train/val/test.
    """
    ordered = _prepare_timestamps(dataframe)
    offline = ordered.loc[
        ordered[TIME_COLUMN] <= OFFLINE_END
    ].copy()
    production = ordered.loc[
        ordered[TIME_COLUMN] >= PRODUCTION_START
    ].copy()

    if offline.empty:
        raise ValueError("Không có dữ liệu offline đến hết năm 2015.")
    return offline, production


def split_development_and_final_test(dataframe):
    """
    Chia offline thành Development và Final Test cố định.

    Development dùng cho cross-validation. Final Test quý IV/2015 chỉ được
    mở sau khi cấu hình model đã được quyết định bằng các fold validation.
    """
    offline, _ = split_offline_and_production(dataframe)
    development = offline.loc[
        offline[TIME_COLUMN] <= DEVELOPMENT_END
    ].copy()
    final_test = offline.loc[
        offline[TIME_COLUMN] >= FINAL_TEST_START
    ].copy()

    if development.empty or final_test.empty:
        raise ValueError(
            "Development hoặc Final Test đang rỗng. "
            "Cần dữ liệu trước và trong quý IV/2015."
        )
    return development, final_test


def create_expanding_window_folds(
    development_df,
    n_splits=DEFAULT_CV_SPLITS,
):
    """
    Tạo expanding-window folds: train luôn ở trước validation.

    TimeSeriesSplit không shuffle. Mỗi fold sau giữ toàn bộ quá khứ của fold
    trước và bổ sung thêm dữ liệu mới vào tập train.
    """
    ordered = _prepare_timestamps(development_df)
    if n_splits < 2:
        raise ValueError("Time-series CV cần ít nhất 2 fold.")
    if len(ordered) <= n_splits:
        raise ValueError("Không đủ dữ liệu để tạo các CV fold.")

    splitter = TimeSeriesSplit(n_splits=n_splits)
    folds = []
    for fold_number, (train_indices, validation_indices) in enumerate(
        splitter.split(ordered),
        start=1,
    ):
        train_df = ordered.iloc[train_indices].copy()
        validation_df = ordered.iloc[validation_indices].copy()
        if (
            train_df[TIME_COLUMN].max()
            >= validation_df[TIME_COLUMN].min()
        ):
            raise ValueError(
                "Fold không hợp lệ: train phải kết thúc trước validation."
            )
        folds.append(
            {
                "fold": fold_number,
                "train": train_df,
                "validation": validation_df,
            }
        )
    return folds


def summarize_time_range(dataframe):
    """Tóm tắt một vùng dữ liệu để ghi vào báo cáo JSON."""
    return {
        "rows": int(len(dataframe)),
        "start": pd.Timestamp(
            dataframe[TIME_COLUMN].min()
        ).isoformat(),
        "end": pd.Timestamp(
            dataframe[TIME_COLUMN].max()
        ).isoformat(),
    }


def timestamps_to_source_indices(source_df, timestamps):
    """Đổi danh sách timestamp thành vị trí dòng trong timeline hourly."""
    source_times = pd.to_datetime(source_df[TIME_COLUMN])
    if source_times.duplicated().any():
        raise ValueError("Timeline hourly có date_time bị trùng.")

    position_by_time = pd.Series(
        np.arange(len(source_df), dtype=np.int64),
        index=source_times,
    )
    requested = pd.DatetimeIndex(pd.to_datetime(timestamps))
    missing = requested.difference(position_by_time.index)
    if not missing.empty:
        raise ValueError(
            "Không tìm thấy timestamp trong timeline hourly: "
            f"{missing[0]}"
        )
    return position_by_time.loc[requested].to_numpy(dtype=np.int64)
