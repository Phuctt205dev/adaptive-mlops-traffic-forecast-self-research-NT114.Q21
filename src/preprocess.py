# src/preprocess.py
import pandas as pd
import numpy as np


def load_data(path):
    df = pd.read_csv(path)
    return df


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