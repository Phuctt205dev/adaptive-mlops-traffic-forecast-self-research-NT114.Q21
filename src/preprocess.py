import pandas as pd

def load_data(path):
    df = pd.read_csv(path)
    return df


def preprocess(df):
    # =========================
    # 1. FIX HOLIDAY
    # =========================
    # is_holiday đang là text (NaN nếu không phải ngày lễ)
    # → chuyển thành 0/1
    df["is_holiday"] = df["is_holiday"].notna().astype(int)

    # =========================
    # 2. CONVERT TIME
    # =========================
    # chuyển sang dạng datetime để xử lý thời gian
    df["date_time"] = pd.to_datetime(df["date_time"])

    # tách các thành phần thời gian
    df["hour"] = df["date_time"].dt.hour          # giờ (0–23)
    df["day"] = df["date_time"].dt.dayofweek      # thứ (0=Mon)
    df["month"] = df["date_time"].dt.month        # tháng

    # =========================
    # 3. WEEKEND FEATURE
    # =========================
    # cuối tuần (thứ 7, CN)
    df["is_weekend"] = df["day"].isin([5, 6]).astype(int)

    # =========================
    # 4. LAG FEATURES (QUAN TRỌNG)
    # =========================
    # ⚠️ Lag = giá trị trong quá khứ của traffic

    # 1 giờ trước
    df["lag_1"] = df["traffic_volume"].shift(1)

    # 24 giờ trước (cùng giờ hôm qua)
    df["lag_24"] = df["traffic_volume"].shift(24)

    # 168 giờ trước (cùng giờ tuần trước)
    df["lag_168"] = df["traffic_volume"].shift(168)

    # =========================
    # 5. ENCODE CATEGORICAL
    # =========================
    # chuyển weather_type thành dạng số (one-hot encoding)
    df = pd.get_dummies(df, columns=["weather_type"], drop_first=True)

    # =========================
    # 6. DROP CỘT KHÔNG CẦN
    # =========================
    # df = df.drop(["date_time", "weather_description"], axis=1)
    df = df.drop(["weather_description"], axis=1)

    # =========================
    # 7. DROP NA (RẤT QUAN TRỌNG)
    # =========================
    # vì lag sẽ tạo ra NaN ở những dòng đầu
    # (ví dụ lag_168 cần 168 dòng trước đó)
    df = df.dropna()

    return df


def split_data(df):
    n = len(df)

    train_end = int(n * 0.7)
    val_end = int(n * 0.85)

    train_df = df[:train_end]
    val_df = df[train_end:val_end]
    test_df = df[val_end:]

    return train_df, val_df, test_df