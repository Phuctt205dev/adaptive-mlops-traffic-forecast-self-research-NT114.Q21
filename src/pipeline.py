import joblib
import os
import random
import numpy as np
import mlflow
import mlflow.sklearn
import mlflow.data

from sklearn.metrics import (
    mean_absolute_error,
    mean_squared_error,
    mean_absolute_percentage_error
)

from src.preprocess import (
    load_data,
    preprocess,
    split_data
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

    mape = mean_absolute_percentage_error(
        y_true,
        y_pred
    ) * 100

    return mae, rmse, mape


def run_pipeline(
    train_start_date,
    train_end_date
):
    print(
        "\n🚀 Retraining model..."
    )

    # =========================
    # FORCE LOCAL MLFLOW
    # =========================
    mlflow.set_tracking_uri(
        "file:./mlruns"
    )

    # =========================
    # SET EXPERIMENT
    # =========================
    mlflow.set_experiment(
        "Traffic Forecast"
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

    # =========================
    # FILTER TRAIN WINDOW
    # =========================
    df = df[
        (df["date_time"] >= train_start_date)
        &
        (df["date_time"] < train_end_date)
    ].copy()

    # =========================
    # SPLIT TRAIN / VAL / TEST
    # (giữ logic file preprocess.py)
    # =========================
    train_part, val_part, test_part = split_data(
        df
    )

    print(
        f"\n📦 Train size: {len(train_part)}"
    )

    print(
        f"📦 Val size  : {len(val_part)}"
    )

    print(
        f"📦 Test size : {len(test_part)}"
    )

    # =========================
    # TRAIN SET
    # =========================
    X_train = train_part.drop(
        ["traffic_volume", "date_time"],
        axis=1
    )

    y_train = train_part[
        "traffic_volume"
    ]

    # =========================
    # VALIDATION SET
    # =========================
    X_val = val_part.drop(
        ["traffic_volume", "date_time"],
        axis=1
    )

    y_val = val_part[
        "traffic_volume"
    ]

    # =========================
    # TEST SET
    # =========================
    X_test = test_part.drop(
        ["traffic_volume", "date_time"],
        axis=1
    )

    y_test = test_part[
        "traffic_volume"
    ]

    # =========================
    # DATASET OBJECT
    # =========================
    mlflow_dataset = mlflow.data.from_pandas(
        df,
        source="data/TrafficVolumeData.csv",
        name="TrafficVolumeData"
    )

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

        with mlflow.start_run(
            run_name=name
        ):

            # =====================
            # LOG DATASET
            # =====================
            mlflow.log_input(
                mlflow_dataset,
                context="training"
            )

            # =====================
            # TRAIN MODEL
            # =====================
            model = func(
                X_train,
                y_train,
                random_state
            )

            # =====================
            # VALIDATION PREDICT
            # =====================
            pred = model.predict(
                X_val
            )

            mae, rmse, mape = evaluate(
                y_val,
                pred
            )

            print(
                f"\n{name}"
            )

            print(
                f"MAE  : {mae:.2f}"
            )

            print(
                f"RMSE : {rmse:.2f}"
            )

            print(
                f"MAPE : {mape:.2f}%"
            )

            # =====================
            # ATTRIBUTE
            # =====================
            mlflow.set_tag(
                "Models",
                name
            )

            # =====================
            # PARAMS
            # =====================
            mlflow.log_param(
                "train_start_date",
                train_start_date
            )

            mlflow.log_param(
                "train_end_date",
                train_end_date
            )

            mlflow.log_param(
                "random_state",
                random_state
            )

            # =====================
            # METRICS
            # =====================
            mlflow.log_metric(
                "MAE",
                mae
            )

            mlflow.log_metric(
                "RMSE",
                rmse
            )

            mlflow.log_metric(
                "MAPE",
                mape
            )

            # =====================
            # LOG MODEL
            # =====================
            mlflow.sklearn.log_model(
                sk_model=model,
                name=name
            )

            results.append(
                (
                    name,
                    model,
                    rmse
                )
            )

    # =========================
    # PICK BEST MODEL
    # =========================
    best = min(
        results,
        key=lambda x: x[2]
    )

    print(
        "\n🏆 BEST:",
        best[0]
    )

    # =========================
    # TEST BEST MODEL
    # (dùng TEST SET thật)
    # =========================
    best_pred = best[1].predict(
        X_test
    )

    best_mae, best_rmse, best_mape = evaluate(
        y_test,
        best_pred
    )

    print(
        "\n🧪 BEST MODEL TEST"
    )

    print(
        f"MAE  : {best_mae:.2f}"
    )

    print(
        f"RMSE : {best_rmse:.2f}"
    )

    print(
        f"MAPE : {best_mape:.2f}%"
    )

    # =========================
    # SAVE MODEL
    # =========================
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

    # =========================
    # BEST MODEL RUN
    # =========================
    with mlflow.start_run(
        run_name="Best_Model"
    ):

        mlflow.log_input(
            mlflow_dataset,
            context="training"
        )

        mlflow.set_tag(
            "Models",
            best[0]
        )

        # =====================
        # TEST METRICS
        # =====================
        mlflow.log_metric(
            "test_MAE",
            best_mae
        )

        mlflow.log_metric(
            "test_RMSE",
            best_rmse
        )

        mlflow.log_metric(
            "test_MAPE",
            best_mape
        )

        mlflow.log_artifact(
            "models/best_model.pkl"
        )

        mlflow.sklearn.log_model(
            sk_model=best[1],
            name="best_model"
        )