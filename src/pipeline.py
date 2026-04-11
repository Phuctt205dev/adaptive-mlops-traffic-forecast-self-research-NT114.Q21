import mlflow
import mlflow.sklearn
import numpy as np

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
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))

    # tránh chia cho 0
    mape = np.mean(np.abs((y_true - y_pred) / (y_true + 1e-5))) * 100

    return mae, rmse, mape


# =========================
# TRAIN + LOG FUNCTION
# =========================
def train_and_log(model_name, train_func, X_train, y_train, X_test, y_test):

    with mlflow.start_run(run_name=model_name):

        model = train_func(X_train, y_train)
        preds = model.predict(X_test)

        mae, rmse, mape = evaluate(y_test, preds)

        # log params
        mlflow.log_param("model", model_name)

        # log metrics
        mlflow.log_metric("mae", mae)
        mlflow.log_metric("rmse", rmse)
        mlflow.log_metric("mape", mape)

        # log model
        mlflow.sklearn.log_model(model, "model")

        print(f"\n📊 {model_name} Evaluation:")
        print(f"MAE  : {mae:.4f}")
        print(f"RMSE : {rmse:.4f}")
        print(f"MAPE : {mape:.2f}%")


# =========================
# MAIN PIPELINE
# =========================
def run_pipeline():

    print("🚀 Running MLflow pipeline...")

    # 1. Load data
    df = load_data("data/TrafficVolumeData.csv")

    # 2. Preprocess
    df = preprocess(df)

    # 3. Split
    train_df, val_df, test_df = split_data(df)

    # 4. X, y
    X_train = train_df.drop("traffic_volume", axis=1)
    y_train = train_df["traffic_volume"]

    X_test = test_df.drop("traffic_volume", axis=1)
    y_test = test_df["traffic_volume"]

    # =========================
    # 🔥 FIX MLflow
    # =========================
    mlflow.set_tracking_uri("file:./mlruns")   # 👈 THÊM DÒNG NÀY
    mlflow.set_experiment("traffic_prediction")

    # 6. Train models
    train_and_log("RandomForest", train_random_forest, X_train, y_train, X_test, y_test)
    train_and_log("XGBoost", train_xgboost, X_train, y_train, X_test, y_test)
    train_and_log("LightGBM", train_lightgbm, X_train, y_train, X_test, y_test)

    print("\n✅ Pipeline finished! Open MLflow UI to view results.")