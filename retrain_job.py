# retrain_job.py
import os
import json
import time
import joblib
import pandas as pd
from datetime import datetime

from src.preprocess import (
    load_data,
    preprocess
)

from src.drift import (
    detect_drift_by_mae
)

from src.pipeline import (
    run_pipeline
)


# =========================
# CONFIG FROM ENV
# =========================
DATA_PATH = os.getenv(
    "DATA_PATH",
    "data/TrafficVolumeData.csv"
)

MODEL_PATH = os.getenv(
    "MODEL_PATH",
    "models/best_model.pkl"
)

STATE_PATH = os.getenv(
    "STATE_PATH",
    "monitoring/drift_state.json"
)

MAE_THRESHOLD = float(
    os.getenv(
        "MAE_THRESHOLD",
        "700"
    )
)

CHECK_INTERVAL_SECONDS = int(
    os.getenv(
        "CHECK_INTERVAL_SECONDS",
        "60"
    )
)

CHECK_WINDOW_DAYS = int(
    os.getenv(
        "CHECK_WINDOW_DAYS",
        "7"
    )
)

DRIFT_START_DATE = os.getenv(
    "DRIFT_START_DATE",
    "2014-01-01"
)

DRIFT_END_LIMIT = os.getenv(
    "DRIFT_END_LIMIT",
    "2014-09-01"
)

INITIAL_TRAIN_START = os.getenv(
    "INITIAL_TRAIN_START",
    "2012-10-02"
)

INITIAL_TRAIN_END = os.getenv(
    "INITIAL_TRAIN_END",
    "2014-01-01"
)

RUN_ONCE = os.getenv(
    "RUN_ONCE",
    "false"
).lower() == "true"


# =========================
# PRINT HELPER
# =========================
def log(message):
    print(
        message,
        flush=True
    )


# =========================
# ENSURE FOLDERS
# =========================
def ensure_folders():
    os.makedirs(
        "monitoring",
        exist_ok=True
    )

    os.makedirs(
        "models",
        exist_ok=True
    )

    os.makedirs(
        "mlruns",
        exist_ok=True
    )

    os.makedirs(
        "data_versions",
        exist_ok=True
    )

    os.makedirs(
        "results",
        exist_ok=True
    )


# =========================
# LOAD STATE
# =========================
def load_state():
    if os.path.exists(
        STATE_PATH
    ):
        with open(
            STATE_PATH,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    return {
        "next_check_start": DRIFT_START_DATE,
        "last_check_start": None,
        "last_check_end": None,
        "last_drift": None,
        "last_mae": None,
        "last_status": "initialized",
        "last_error": None,
        "run_count": 0,
        "updated_at": datetime.now().isoformat()
    }


# =========================
# SAVE STATE
# =========================
def save_state(state):
    state["updated_at"] = datetime.now().isoformat()

    os.makedirs(
        os.path.dirname(STATE_PATH),
        exist_ok=True
    )

    with open(
        STATE_PATH,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            state,
            f,
            indent=4,
            ensure_ascii=False
        )


# =========================
# GET LATEST MODEL VERSION
# =========================
def get_latest_model_version():
    version_file = (
        "models/model_versions.csv"
    )

    if not os.path.exists(
        version_file
    ):
        return "unknown"

    try:
        df = pd.read_csv(
            version_file
        )

        if len(df) == 0:
            return "unknown"

        return str(
            df.iloc[-1]["version"]
        )

    except Exception:
        return "unknown"


# =========================
# PRINT BEST MODEL INFO
# =========================
def print_best_model_info():
    info_path = (
        "models/best_model_info.json"
    )

    if not os.path.exists(
        info_path
    ):
        log(
            "ℹ️ No best_model_info.json found yet."
        )
        return

    try:
        with open(
            info_path,
            "r",
            encoding="utf-8"
        ) as f:
            info = json.load(f)

        log(
            "\n🏆 CURRENT BEST MODEL INFO"
        )

        log(
            f"Model type   : {info.get('best_model_name')}"
        )

        log(
            f"Model version: {info.get('model_version')}"
        )

        log(
            f"Data version : {info.get('data_version')}"
        )

        log(
            f"Test MAE     : {info.get('test_MAE')}"
        )

        log(
            f"Test RMSE    : {info.get('test_RMSE')}"
        )

        log(
            f"Test MAPE    : {info.get('test_MAPE')}"
        )

    except Exception as e:
        log(
            f"⚠️ Cannot read best model info: {e}"
        )


# =========================
# TRAIN INITIAL MODEL IF MISSING
# =========================
def train_initial_model_if_missing():
    if os.path.exists(
        MODEL_PATH
    ):
        return

    log(
        "\n⚠️ No model found."
    )

    log(
        "🚀 Training initial model..."
    )

    log(
        f"Train window: {INITIAL_TRAIN_START} → {INITIAL_TRAIN_END}"
    )

    run_pipeline(
        train_start_date=INITIAL_TRAIN_START,
        train_end_date=INITIAL_TRAIN_END
    )

    log(
        "✅ Initial model created."
    )

    print_best_model_info()


# =========================
# CHECK DRIFT ONCE
# =========================
def check_drift_once():
    ensure_folders()

    log(
        "\n=============================="
    )

    log(
        "🔍 DRIFT WORKER START CHECK"
    )

    log(
        "=============================="
    )

    log(
        f"DATA_PATH: {DATA_PATH}"
    )

    log(
        f"MODEL_PATH: {MODEL_PATH}"
    )

    log(
        f"MAE_THRESHOLD: {MAE_THRESHOLD}"
    )

    log(
        f"CHECK_WINDOW_DAYS: {CHECK_WINDOW_DAYS}"
    )

    train_initial_model_if_missing()

    print_best_model_info()

    state = load_state()

    check_start = pd.to_datetime(
        state["next_check_start"]
    )

    drift_end_limit = pd.to_datetime(
        DRIFT_END_LIMIT
    )

    if check_start >= drift_end_limit:
        log(
            "\n✅ All drift windows have been checked."
        )

        log(
            f"Current next_check_start: {check_start}"
        )

        state["last_status"] = "finished_all_windows"

        save_state(
            state
        )

        return

    check_end = check_start + pd.Timedelta(
        days=CHECK_WINDOW_DAYS
    )

    if check_end > drift_end_limit:
        check_end = drift_end_limit

    log(
        "\n📦 Drift check window:"
    )

    log(
        f"{check_start} → {check_end}"
    )

    state["last_check_start"] = str(
        check_start.date()
    )

    state["last_check_end"] = str(
        check_end.date()
    )

    state["last_status"] = "checking_drift"

    state["last_error"] = None

    save_state(
        state
    )

    df = load_data(
        DATA_PATH
    )

    df = preprocess(
        df
    )

    df = df.sort_values(
        "date_time"
    )

    data_min = df["date_time"].min()
    data_max = df["date_time"].max()

    log(
        "\n📊 Dataset time range:"
    )

    log(
        f"{data_min} → {data_max}"
    )

    current_window = df[
        (df["date_time"] >= check_start)
        &
        (df["date_time"] < check_end)
    ].copy()

    if len(current_window) == 0:
        log(
            "\n⚠️ No data found in this drift window."
        )

        state["next_check_start"] = str(
            check_end.date()
        )

        state["last_status"] = "no_data"

        state["run_count"] = int(
            state.get(
                "run_count",
                0
            )
        ) + 1

        save_state(
            state
        )

        return

    log(
        f"\n📦 Drift check rows: {len(current_window)}"
    )

    log(
        "\n📦 Loading current model..."
    )

    model = joblib.load(
        MODEL_PATH
    )

    log(
        "✅ Model loaded"
    )

    X = current_window.drop(
        [
            "traffic_volume",
            "date_time"
        ],
        axis=1
    )

    X = X.reindex(
        columns=model.feature_names_in_,
        fill_value=0
    )

    y_true = current_window[
        "traffic_volume"
    ]

    log(
        "\n🔮 Predicting drift window..."
    )

    y_pred = model.predict(
        X
    )

    model_version = get_latest_model_version()

    drift, mae = detect_drift_by_mae(
        y_true,
        y_pred,
        mae_threshold=MAE_THRESHOLD,
        model_version=model_version
    )

    state["last_drift"] = bool(
        drift
    )

    state["last_mae"] = float(
        mae
    )

    state["last_status"] = (
        "drift_detected"
        if drift
        else "no_drift"
    )

    save_state(
        state
    )

    if drift:
        log(
            "\n🚨 Drift found → retraining model"
        )

        new_train_start = INITIAL_TRAIN_START

        new_train_end = str(
            check_end.date()
        )

        log(
            "\n📦 New train window:"
        )

        log(
            f"{new_train_start} → {new_train_end}"
        )

        state["last_status"] = "retraining"

        save_state(
            state
        )

        run_pipeline(
            train_start_date=new_train_start,
            train_end_date=new_train_end
        )

        log(
            "\n✅ Retrain complete."
        )

        state["last_status"] = "retrain_complete"

        print_best_model_info()

    else:
        log(
            "\n✅ No drift → keep current model."
        )

        state["last_status"] = "no_drift_keep_model"

    state["next_check_start"] = str(
        check_end.date()
    )

    state["run_count"] = int(
        state.get(
            "run_count",
            0
        )
    ) + 1

    save_state(
        state
    )

    log(
        "\n📝 Drift state saved."
    )

    log(
        f"Next check starts at: {state['next_check_start']}"
    )


# =========================
# MAIN LOOP
# =========================
if __name__ == "__main__":

    ensure_folders()

    log(
        "\n🚀 Traffic Drift Worker Started"
    )

    log(
        f"RUN_ONCE: {RUN_ONCE}"
    )

    log(
        f"CHECK_INTERVAL_SECONDS: {CHECK_INTERVAL_SECONDS}"
    )

    while True:

        try:

            check_drift_once()

        except Exception as e:

            log(
                "\n❌ Drift worker error:"
            )

            log(
                str(e)
            )

            state = load_state()

            state["last_status"] = "error"

            state["last_error"] = str(
                e
            )

            save_state(
                state
            )

        if RUN_ONCE:
            log(
                "\n✅ RUN_ONCE=true → worker stopped."
            )
            break

        log(
            f"\n⏳ Sleeping {CHECK_INTERVAL_SECONDS} seconds..."
        )

        time.sleep(
            CHECK_INTERVAL_SECONDS
        )