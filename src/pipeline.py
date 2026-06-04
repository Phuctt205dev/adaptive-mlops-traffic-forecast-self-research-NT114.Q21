# # src/pipeline.py
# import joblib
# import os
# import random
# import numpy as np
# import pandas as pd
# import mlflow
# import mlflow.sklearn
# import mlflow.data

# from sklearn.metrics import (
#     mean_absolute_error,
#     mean_squared_error,
#     mean_absolute_percentage_error
# )

# from src.preprocess import (
#     load_data,
#     preprocess,
#     split_data
# )

# from src.train import (
#     train_random_forest,
#     train_xgboost,
#     train_lightgbm
# )


# def evaluate(
#     y_true,
#     y_pred
# ):
#     mae = mean_absolute_error(
#         y_true,
#         y_pred
#     )

#     mse = mean_squared_error(
#         y_true,
#         y_pred
#     )

#     rmse = np.sqrt(
#         mse
#     )

#     mape = mean_absolute_percentage_error(
#         y_true,
#         y_pred
#     ) * 100

#     return mae, rmse, mape


# # ====================================
# # CREATE DATA VERSION
# # ====================================
# def create_data_version(
#     df,
#     train_start_date,
#     train_end_date
# ):
#     os.makedirs(
#         "data_versions",
#         exist_ok=True
#     )

#     log_path = (
#         "data_versions/version_log.csv"
#     )

#     if os.path.exists(
#         log_path
#     ):
#         log_df = pd.read_csv(
#             log_path
#         )

#         version_num = (
#             len(log_df) + 1
#         )

#     else:
#         log_df = pd.DataFrame(
#             columns=[
#                 "version",
#                 "train_start",
#                 "train_end",
#                 "rows"
#             ]
#         )

#         version_num = 1

#     version_name = (
#         f"data_v{version_num}"
#     )

#     version_file = (
#         f"data_versions/{version_name}.csv"
#     )

#     df.to_csv(
#         version_file,
#         index=False
#     )

#     new_row = pd.DataFrame(
#         [{
#             "version":
#             version_name,

#             "train_start":
#             train_start_date,

#             "train_end":
#             train_end_date,

#             "rows":
#             len(df)
#         }]
#     )

#     log_df = pd.concat(
#         [
#             log_df,
#             new_row
#         ],
#         ignore_index=True
#     )

#     log_df.to_csv(
#         log_path,
#         index=False
#     )

#     print(
#         "\n📦 Data version created:"
#     )

#     print(
#         version_name
#     )

#     return version_name


# # ====================================
# # CREATE MODEL VERSION
# # ====================================
# def create_model_version():
#     os.makedirs(
#         "models",
#         exist_ok=True
#     )

#     version_file = (
#         "models/model_versions.csv"
#     )

#     if os.path.exists(
#         version_file
#     ):
#         df = pd.read_csv(
#             version_file
#         )

#         version_num = (
#             len(df) + 1
#         )

#     else:
#         df = pd.DataFrame(
#             columns=[
#                 "version"
#             ]
#         )

#         version_num = 1

#     model_version = (
#         f"model_v{version_num}"
#     )

#     new_row = pd.DataFrame(
#         [{
#             "version":
#             model_version
#         }]
#     )

#     df = pd.concat(
#         [
#             df,
#             new_row
#         ],
#         ignore_index=True
#     )

#     df.to_csv(
#         version_file,
#         index=False
#     )

#     print(
#         "\n🧠 Model version:"
#     )

#     print(
#         model_version
#     )

#     return model_version


# def run_pipeline(
#     train_start_date,
#     train_end_date
# ):
#     print(
#         "\n🚀 Retraining model..."
#     )

#     mlflow.set_tracking_uri(
#         "file:./mlruns"
#     )

#     mlflow.set_experiment(
#         "Traffic Forecast"
#     )

#     random_state = random.randint(
#         1,
#         100000
#     )

#     print(
#         "🎲 Random state:",
#         random_state
#     )

#     df = load_data(
#         "data/TrafficVolumeData.csv"
#     )

#     df = preprocess(
#         df
#     )

#     df = df[
#         (df["date_time"] >= train_start_date)
#         &
#         (df["date_time"] < train_end_date)
#     ].copy()

#     data_version = create_data_version(
#         df,
#         train_start_date,
#         train_end_date
#     )

#     train_part, val_part, test_part = split_data(
#         df
#     )

#     print(
#         f"\n📦 Train size: {len(train_part)}"
#     )

#     print(
#         f"📦 Val size  : {len(val_part)}"
#     )

#     print(
#         f"📦 Test size : {len(test_part)}"
#     )

#     X_train = train_part.drop(
#         ["traffic_volume", "date_time"],
#         axis=1
#     )

#     y_train = train_part[
#         "traffic_volume"
#     ]

#     X_val = val_part.drop(
#         ["traffic_volume", "date_time"],
#         axis=1
#     )

#     y_val = val_part[
#         "traffic_volume"
#     ]

#     X_test = test_part.drop(
#         ["traffic_volume", "date_time"],
#         axis=1
#     )

#     y_test = test_part[
#         "traffic_volume"
#     ]

#     mlflow_dataset = mlflow.data.from_pandas(
#         df,
#         source="data/TrafficVolumeData.csv",
#         name="TrafficVolumeData"
#     )

#     results = []

#     for name, func in [

#         (
#             "RandomForest",
#             train_random_forest
#         ),

#         (
#             "XGBoost",
#             train_xgboost
#         ),

#         (
#             "LightGBM",
#             train_lightgbm
#         ),
#     ]:

#         with mlflow.start_run(
#             run_name=name
#         ):

#             mlflow.log_input(
#                 mlflow_dataset,
#                 context="training"
#             )

#             model = func(
#                 X_train,
#                 y_train,
#                 random_state
#             )

#             pred = model.predict(
#                 X_val
#             )

#             mae, rmse, mape = evaluate(
#                 y_val,
#                 pred
#             )

#             print(
#                 f"\n{name}"
#             )

#             print(
#                 f"MAE  : {mae:.2f}"
#             )

#             print(
#                 f"RMSE : {rmse:.2f}"
#             )

#             print(
#                 f"MAPE : {mape:.2f}%"
#             )

#             mlflow.set_tag(
#                 "Models",
#                 name
#             )

#             mlflow.set_tag(
#                 "data_version",
#                 data_version
#             )

#             mlflow.log_param(
#                 "train_start_date",
#                 train_start_date
#             )

#             mlflow.log_param(
#                 "train_end_date",
#                 train_end_date
#             )

#             mlflow.log_param(
#                 "random_state",
#                 random_state
#             )

#             mlflow.log_metric(
#                 "MAE",
#                 mae
#             )

#             mlflow.log_metric(
#                 "RMSE",
#                 rmse
#             )

#             mlflow.log_metric(
#                 "MAPE",
#                 mape
#             )

#             mlflow.sklearn.log_model(
#                 sk_model=model,
#                 name=name
#             )

#             results.append(
#                 (
#                     name,
#                     model,
#                     rmse
#                 )
#             )

#     best = min(
#         results,
#         key=lambda x: x[2]
#     )

#     print(
#         "\n🏆 BEST:",
#         best[0]
#     )

#     model_version = create_model_version()

#     best_pred = best[1].predict(
#         X_test
#     )

#     best_mae, best_rmse, best_mape = evaluate(
#         y_test,
#         best_pred
#     )

#     print(
#         "\n🧪 BEST MODEL TEST"
#     )

#     print(
#         f"MAE  : {best_mae:.2f}"
#     )

#     print(
#         f"RMSE : {best_rmse:.2f}"
#     )

#     print(
#         f"MAPE : {best_mape:.2f}%"
#     )

#     os.makedirs(
#         "models",
#         exist_ok=True
#     )

#     joblib.dump(
#         best[1],
#         "models/best_model.pkl"
#     )

#     joblib.dump(
#         best[1],
#         f"models/{model_version}.pkl"
#     )

#     print(
#         "✅ Model saved"
#     )

#     with mlflow.start_run(
#         run_name="Best_Model"
#     ):

#         mlflow.log_input(
#             mlflow_dataset,
#             context="training"
#         )

#         mlflow.set_tag(
#             "Models",
#             best[0]
#         )

#         mlflow.set_tag(
#             "data_version",
#             data_version
#         )

#         mlflow.set_tag(
#             "model_version",
#             model_version
#         )

#         mlflow.log_metric(
#             "test_MAE",
#             best_mae
#         )

#         mlflow.log_metric(
#             "test_RMSE",
#             best_rmse
#         )

#         mlflow.log_metric(
#             "test_MAPE",
#             best_mape
#         )

#         mlflow.log_artifact(
#             "models/best_model.pkl"
#         )

#         mlflow.sklearn.log_model(
#             sk_model=best[1],
#             name=best[0]
#         )




# src/pipeline.py
import joblib
import os
import json
import random
import numpy as np
import pandas as pd
import mlflow
import mlflow.sklearn
import mlflow.data

from datetime import datetime

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


# =========================
# MLflow file store allow
# =========================
os.environ.setdefault(
    "MLFLOW_ALLOW_FILE_STORE",
    "true"
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


# ====================================
# CREATE DATA VERSION
# ====================================
def create_data_version(
    df,
    train_start_date,
    train_end_date
):
    os.makedirs(
        "data_versions",
        exist_ok=True
    )

    log_path = (
        "data_versions/version_log.csv"
    )

    if os.path.exists(
        log_path
    ):
        log_df = pd.read_csv(
            log_path
        )

        version_num = (
            len(log_df) + 1
        )

    else:
        log_df = pd.DataFrame(
            columns=[
                "version",
                "train_start",
                "train_end",
                "rows"
            ]
        )

        version_num = 1

    version_name = (
        f"data_v{version_num}"
    )

    version_file = (
        f"data_versions/{version_name}.csv"
    )

    df.to_csv(
        version_file,
        index=False
    )

    new_row = pd.DataFrame(
        [{
            "version":
            version_name,

            "train_start":
            train_start_date,

            "train_end":
            train_end_date,

            "rows":
            len(df)
        }]
    )

    log_df = pd.concat(
        [
            log_df,
            new_row
        ],
        ignore_index=True
    )

    log_df.to_csv(
        log_path,
        index=False
    )

    print(
        "\n📦 Data version created:"
    )

    print(
        version_name
    )

    return version_name


# ====================================
# CREATE MODEL VERSION
# ====================================
def create_model_version():
    os.makedirs(
        "models",
        exist_ok=True
    )

    version_file = (
        "models/model_versions.csv"
    )

    if os.path.exists(
        version_file
    ):
        df = pd.read_csv(
            version_file
        )

        version_num = (
            len(df) + 1
        )

    else:
        df = pd.DataFrame(
            columns=[
                "version"
            ]
        )

        version_num = 1

    model_version = (
        f"model_v{version_num}"
    )

    new_row = pd.DataFrame(
        [{
            "version":
            model_version
        }]
    )

    df = pd.concat(
        [
            df,
            new_row
        ],
        ignore_index=True
    )

    df.to_csv(
        version_file,
        index=False
    )

    print(
        "\n🧠 Model version:"
    )

    print(
        model_version
    )

    return model_version


# ====================================
# SAVE BEST MODEL INFO
# ====================================
def save_best_model_info(
    best_model_name,
    model_version,
    data_version,
    train_start_date,
    train_end_date,
    test_mae,
    test_rmse,
    test_mape
):
    os.makedirs(
        "models",
        exist_ok=True
    )

    info = {
        "best_model_name": best_model_name,
        "model_version": model_version,
        "data_version": data_version,
        "train_start_date": train_start_date,
        "train_end_date": train_end_date,
        "test_MAE": round(float(test_mae), 4),
        "test_RMSE": round(float(test_rmse), 4),
        "test_MAPE": round(float(test_mape), 4),
        "saved_at": datetime.now().isoformat(),
        "model_file": "models/best_model.pkl"
    }

    info_path = (
        "models/best_model_info.json"
    )

    with open(
        info_path,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            info,
            f,
            indent=4,
            ensure_ascii=False
        )

    print(
        "\n📝 Best model info saved:"
    )

    print(
        info_path
    )

    print(
        f"🏆 Best model type: {best_model_name}"
    )


def run_pipeline(
    train_start_date,
    train_end_date
):
    print(
        "\n🚀 Retraining model..."
    )

    os.environ.setdefault(
        "MLFLOW_ALLOW_FILE_STORE",
        "true"
    )

    mlflow.set_tracking_uri(
        "file:./mlruns"
    )

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

    df = df[
        (df["date_time"] >= train_start_date)
        &
        (df["date_time"] < train_end_date)
    ].copy()

    data_version = create_data_version(
        df,
        train_start_date,
        train_end_date
    )

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

    X_test = test_part.drop(
        ["traffic_volume", "date_time"],
        axis=1
    )

    y_test = test_part[
        "traffic_volume"
    ]

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

            mlflow.log_input(
                mlflow_dataset,
                context="training"
            )

            model = func(
                X_train,
                y_train,
                random_state
            )

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

            mlflow.set_tag(
                "Models",
                name
            )

            mlflow.set_tag(
                "data_version",
                data_version
            )

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

    best = min(
        results,
        key=lambda x: x[2]
    )

    print(
        "\n🏆 BEST:",
        best[0]
    )

    model_version = create_model_version()

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

    os.makedirs(
        "models",
        exist_ok=True
    )

    joblib.dump(
        best[1],
        "models/best_model.pkl"
    )

    joblib.dump(
        best[1],
        f"models/{model_version}.pkl"
    )

    save_best_model_info(
        best_model_name=best[0],
        model_version=model_version,
        data_version=data_version,
        train_start_date=train_start_date,
        train_end_date=train_end_date,
        test_mae=best_mae,
        test_rmse=best_rmse,
        test_mape=best_mape
    )

    print(
        "✅ Model saved"
    )

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

        mlflow.set_tag(
            "data_version",
            data_version
        )

        mlflow.set_tag(
            "model_version",
            model_version
        )

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

        mlflow.log_artifact(
            "models/best_model_info.json"
        )

        mlflow.sklearn.log_model(
            sk_model=best[1],
            name=best[0]
        )