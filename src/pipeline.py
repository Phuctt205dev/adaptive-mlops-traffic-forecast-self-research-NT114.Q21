# src/pipeline.py
import joblib
import os
import random
import numpy as np

from sklearn.metrics import (
    mean_absolute_error,
    mean_squared_error
)

from src.preprocess import (
    load_data,
    preprocess
)

from src.train import (
    train_random_forest,
    train_xgboost,
    train_lightgbm
)


def evaluate(
    y_true,
    y_pred
):
    mae = mean_absolute_error(
        y_true,
        y_pred
    )

    mse = mean_squared_error(
        y_true,
        y_pred
    )

    rmse = np.sqrt(
        mse
    )

    return mae, rmse


def run_pipeline(
    train_start_date,
    train_end_date
):
    print(
        "\n🚀 Retraining model..."
    )

    random_state = random.randint(
        1,
        100000
    )

    print(
        "🎲 Random state:",
        random_state
    )

    df = load_data(
        "data/TrafficVolumeData.csv"
    )

    df = preprocess(
        df
    )

    train_df = df[
        (df["date_time"] >= train_start_date)
        &
        (df["date_time"] < train_end_date)
    ].copy()

    n = len(
        train_df
    )

    split_idx = int(
        n * 0.85
    )

    train_part = train_df[
        :split_idx
    ]

    val_part = train_df[
        split_idx:
    ]

    X_train = train_part.drop(
        ["traffic_volume", "date_time"],
        axis=1
    )

    y_train = train_part[
        "traffic_volume"
    ]

    X_val = val_part.drop(
        ["traffic_volume", "date_time"],
        axis=1
    )

    y_val = val_part[
        "traffic_volume"
    ]

    results = []

    for name, func in [

        (
            "RandomForest",
            train_random_forest
        ),

        (
            "XGBoost",
            train_xgboost
        ),

        (
            "LightGBM",
            train_lightgbm
        ),
    ]:

        model = func(
            X_train,
            y_train,
            random_state
        )

        pred = model.predict(
            X_val
        )

        mae, rmse = evaluate(
            y_val,
            pred
        )

        print(
            f"{name}"
        )

        print(
            f"MAE  : {mae:.2f}"
        )

        print(
            f"RMSE : {rmse:.2f}"
        )

        results.append(
            (
                name,
                model,
                rmse
            )
        )

    best = min(
        results,
        key=lambda x: x[2]
    )

    print(
        "\n🏆 BEST:",
        best[0]
    )

    os.makedirs(
        "models",
        exist_ok=True
    )

    joblib.dump(
        best[1],
        "models/best_model.pkl"
    )

    print(
        "✅ Model saved"
    )