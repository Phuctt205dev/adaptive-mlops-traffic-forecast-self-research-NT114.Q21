import pandas as pd

def load_data(path):
    df = pd.read_csv(path)
    return df


def preprocess(df):
    # 1. Fix holiday
    df["is_holiday"] = df["is_holiday"].notna().astype(int)

    # 2. Convert time
    df["date_time"] = pd.to_datetime(df["date_time"])
    df["hour"] = df["date_time"].dt.hour
    df["day"] = df["date_time"].dt.dayofweek
    df["month"] = df["date_time"].dt.month

    # 3. Weekend feature
    df["is_weekend"] = df["day"].isin([5, 6]).astype(int)

    # 4. Encode categorical
    df = pd.get_dummies(df, columns=["weather_type"], drop_first=True)

    # 5. Drop useless
    df = df.drop(["date_time", "weather_description"], axis=1)

    return df


def split_data(df):
    n = len(df)

    train_end = int(n * 0.7)
    val_end = int(n * 0.85)

    train_df = df[:train_end]
    val_df = df[train_end:val_end]
    test_df = df[val_end:]

    return train_df, val_df, test_df