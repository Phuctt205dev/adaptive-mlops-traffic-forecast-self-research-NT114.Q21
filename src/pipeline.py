# src/pipeline.py

import mlflow
import mlflow.sklearn
import numpy as np
import joblib
import os
import random

from sklearn.metrics import mean_absolute_error, mean_squared_error

from src.preprocess import load_data, preprocess, split_data
from src.train import (
    train_random_forest,
    train_xgboost,
    train_lightgbm
)


# =========================
# EVALUATE FUNCTION
# =========================
def evaluate(y_true, y_pred):
    mae = mean_absolute_error(y_true, y_pred)
    mse = mean_squared_error(y_true, y_pred)
    rmse = np.sqrt(mse)
    mape = np.mean(np.abs((y_true - y_pred) / (y_true + 1e-5))) * 100
    return mae, mse, rmse, mape


# =========================
# TRAIN + LOG (VALIDATION)
# =========================
def train_and_log(
    model_name,
    train_func,
    X_train,
    y_train,
    X_val,
    y_val,
    random_state
):

    with mlflow.start_run(run_name=model_name):

        model = train_func(
            X_train,
            y_train,
            random_state
        )

        preds = model.predict(X_val)

        mae, mse, rmse, mape = evaluate(y_val, preds)

        mlflow.log_param("model", model_name)
        mlflow.log_param("random_state", random_state)

        mlflow.log_metric("val_mae", mae)
        mlflow.log_metric("val_mse", mse)
        mlflow.log_metric("val_rmse", rmse)
        mlflow.log_metric("val_mape", mape)

        mlflow.sklearn.log_model(model, model_name)

        print(f"\n📊 {model_name} (VALIDATION):")
        print(f"MAE  : {mae:.4f}")
        print(f"MSE  : {mse:.4f}")
        print(f"RMSE : {rmse:.4f}")
        print(f"MAPE : {mape:.2f}%")

        return {
            "name": model_name,
            "model": model,
            "rmse": rmse
        }


# =========================
# MAIN PIPELINE
# =========================
def run_pipeline():

    print("🚀 Running MLflow pipeline...")

    random_state = random.randint(1, 100000)
    print(f"🎲 Random state: {random_state}")

    # 1. Load + preprocess
    df = load_data("data/TrafficVolumeData.csv")
    df = preprocess(df)

    # 2. Split
    train_df, val_df, test_df = split_data(df)

    # =========================
    # DROP TARGET + DATE
    # =========================
    X_train = train_df.drop(["traffic_volume", "date_time"], axis=1)
    y_train = train_df["traffic_volume"]

    X_val = val_df.drop(["traffic_volume", "date_time"], axis=1)
    y_val = val_df["traffic_volume"]

    X_test = test_df.drop(["traffic_volume", "date_time"], axis=1)
    y_test = test_df["traffic_volume"]

    # =========================
    # MLflow setup
    # =========================
    mlflow.set_tracking_uri("file:./mlruns")
    mlflow.set_experiment("traffic_prediction")

    # =========================
    # TRAIN MODELS
    # =========================
    results = []

    results.append(
        train_and_log(
            "RandomForest",
            train_random_forest,
            X_train,
            y_train,
            X_val,
            y_val,
            random_state
        )
    )

    results.append(
        train_and_log(
            "XGBoost",
            train_xgboost,
            X_train,
            y_train,
            X_val,
            y_val,
            random_state
        )
    )

    results.append(
        train_and_log(
            "LightGBM",
            train_lightgbm,
            X_train,
            y_train,
            X_val,
            y_val,
            random_state
        )
    )

    # =========================
    # CHỌN BEST (VAL)
    # =========================
    best = min(results, key=lambda x: x["rmse"])

    print("\n🏆 BEST MODEL (from validation):", best["name"])

    # =========================
    # FINAL TEST
    # =========================
    preds = best["model"].predict(X_test)

    mae, mse, rmse, mape = evaluate(y_test, preds)

    print("\n📊 FINAL TEST (2013):")
    print(f"MAE  : {mae:.4f}")
    print(f"MSE  : {mse:.4f}")
    print(f"RMSE : {rmse:.4f}")
    print(f"MAPE : {mape:.2f}%")

    # =========================
    # LOG BEST MODEL
    # =========================
    with mlflow.start_run(run_name="Best_Model"):

        mlflow.log_param("best_model", best["name"])
        mlflow.log_param("random_state", random_state)

        mlflow.log_metric("test_mae", mae)
        mlflow.log_metric("test_mse", mse)
        mlflow.log_metric("test_rmse", rmse)
        mlflow.log_metric("test_mape", mape)

        mlflow.sklearn.log_model(best["model"], "best_model")

    # =========================
    # SAVE MODEL
    # =========================
    os.makedirs("models", exist_ok=True)
    joblib.dump(best["model"], "models/best_model.pkl")

    print("\n💾 Saved best model")
    print("\n✅ Pipeline finished!")