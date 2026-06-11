# src/preprocess.py
import os

import pandas as pd
import numpy as np


DEFAULT_AUDIT_PATH = (
    "data/processed/TrafficVolumeData_hourly_audit.csv"
)


def load_data(path):
    df = pd.read_csv(path)
    return df


def load_observed_target_data(
    path,
    audit_path=DEFAULT_AUDIT_PATH,
):
    """
    Đọc dữ liệu huấn luyện nhưng không dùng traffic được suy luận làm nhãn thật.

    File chính vẫn giữ đúng schema gốc. Thông tin giờ thật/giờ suy luận được
    đặt ở file audit riêng. Dòng mới chưa xuất hiện trong audit được xem là dữ
    liệu production thật và vẫn được giữ lại.
    """
    df = load_data(path)
    if not audit_path or not os.path.exists(audit_path):
        return df

    audit_df = pd.read_csv(
        audit_path,
        usecols=["date_time", "target_observed"],
    )
    audit_df["date_time"] = pd.to_datetime(
        audit_df["date_time"],
        errors="raise",
    )
    audit_df["target_observed"] = (
        audit_df["target_observed"]
        .astype(str)
        .str.strip()
        .str.lower()
        .map({"true": True, "false": False})
    )

    if audit_df["target_observed"].isna().any():
        raise ValueError(
            "Audit contains an invalid target_observed value."
        )
    if audit_df["date_time"].duplicated().any():
        raise ValueError(
            "Audit contains duplicate date_time values."
        )

    timestamps = pd.to_datetime(
        df["date_time"],
        errors="raise",
    )
    inferred_timestamps = set(
        audit_df.loc[
            ~audit_df["target_observed"],
            "date_time",
        ]
    )
    return df.loc[
        ~timestamps.isin(inferred_timestamps)
    ].copy()


def preprocess(df):
    # =========================
    # 1. FIX HOLIDAY
    # =========================
    df["is_holiday"] = df["is_holiday"].notna().astype(int)

    # =========================
    # 2. CONVERT TIME
    # =========================
    df["date_time"] = pd.to_datetime(df["date_time"])

    df["hour"] = df["date_time"].dt.hour
    df["day"] = df["date_time"].dt.dayofweek
    df["month"] = df["date_time"].dt.month

    # =========================
    # 🔥 CYCLICAL TIME
    # =========================
    df["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24)

    # =========================
    # 3. WEEKEND FEATURE
    # =========================
    df["is_weekend"] = df["day"].isin([5, 6]).astype(int)

    # =========================
    # 4. ENCODE CATEGORICAL
    # =========================
    df = pd.get_dummies(
        df,
        columns=["weather_type"],
        drop_first=True
    )

    # =========================
    # ❗ CHỈ DROP DESCRIPTION
    # =========================
    df = df.drop(
        ["weather_description"],
        axis=1,
        errors="ignore"
    )

    # =========================
    # 5. DROP NA
    # =========================
    df = df.dropna()

    return df


def split_data(df):
    # =========================
    # 🔥 TIME-BASED SPLIT
    # =========================
    # GIỮ LOGIC CŨ:
    # - sort theo thời gian
    # - chia 70% train, 15% val, 15% test
    #
    # UPDATE:
    # - bỏ filter cứng date_time < 2014-01-01
    # - lý do: khi drift worker retrain với dữ liệu 2014,
    #   model cần được phép học thêm dữ liệu mới.
    # =========================

    df = df.sort_values(
        "date_time"
    )

    n = len(df)

    train_end = int(
        n * 0.7
    )

    val_end = int(
        n * 0.85
    )

    train_df = df[
        :train_end
    ]

    val_df = df[
        train_end:val_end
    ]

    test_df = df[
        val_end:
    ]

    return train_df, val_df, test_df
