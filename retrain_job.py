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

DEGRADATION_RATIO = float(
    os.getenv(
        "DEGRADATION_RATIO",
        "1.5"
    )
)

CHECK_INTERVAL_SECONDS = int(
    os.getenv(
        "CHECK_INTERVAL_SECONDS",
        "60"
    )
)

CHECK_WINDOW_MONTHS = int(
    os.getenv(
        "CHECK_WINDOW_MONTHS",
        "1"
    )
)

TRAIN_WINDOW_MONTHS = int(
    os.getenv(
        "TRAIN_WINDOW_MONTHS",
        "3"
    )
)

DRIFT_START_DATE = os.getenv(
    "DRIFT_START_DATE",
    "2013-04-01"
)

DRIFT_END_LIMIT = os.getenv(
    "DRIFT_END_LIMIT",
    "2014-09-01"
)

INITIAL_TRAIN_START = os.getenv(
    "INITIAL_TRAIN_START",
    "2013-01-01"
)

INITIAL_TRAIN_END = os.getenv(
    "INITIAL_TRAIN_END",
    "2013-04-01"
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
# DATE HELPER
# =========================
def to_date_string(value):
    return str(
        pd.to_datetime(value).date()
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
# DEFAULT STATE
# =========================
def get_default_state():
    return {
        "next_check_start": DRIFT_START_DATE,

        "candidate_train_start": INITIAL_TRAIN_START,
        "candidate_train_end": INITIAL_TRAIN_END,

        "model_train_start": INITIAL_TRAIN_START,
        "model_train_end": INITIAL_TRAIN_END,

        "last_check_start": None,
        "last_check_end": None,

        "last_candidate_train_start": None,
        "last_candidate_train_end": None,

        "last_drift": None,
        "last_mae": None,
        "last_baseline_mae": None,
        "last_ratio_threshold": None,

        "last_status": "initialized",
        "last_error": None,

        "run_count": 0,
        "updated_at": datetime.now().isoformat()
    }


# =========================
# NORMALIZE STATE
# =========================
def normalize_state(state):
    default_state = get_default_state()

    for key, value in default_state.items():
        if key not in state:
            state[key] = value

    return state


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
            state = json.load(f)

        return normalize_state(
            state
        )

    return get_default_state()


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
# GET BEST MODEL INFO
# =========================
def get_best_model_info():
    info_path = (
        "models/best_model_info.json"
    )

    if not os.path.exists(
        info_path
    ):
        return None

    try:
        with open(
            info_path,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception:
        return None


# =========================
# GET BASELINE MAE
# =========================
def get_baseline_mae():
    info = get_best_model_info()

    if info is None:
        return None

    possible_keys = [
        "test_MAE",
        "MAE",
        "mae",
        "test_mae"
    ]

    for key in possible_keys:
        if key in info:
            try:
                return float(
                    info[key]
                )
            except Exception:
                continue

    return None


# =========================
# PRINT BEST MODEL INFO
# =========================
def print_best_model_info():
    info = get_best_model_info()

    if info is None:
        log(
            "ℹ️ No best_model_info.json found yet."
        )
        return

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


# =========================
# TRAIN INITIAL MODEL IF MISSING
# =========================
def train_initial_model_if_missing():
    if os.path.exists(
        MODEL_PATH
    ):
        return False

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

    state = load_state()

    state["model_train_start"] = INITIAL_TRAIN_START
    state["model_train_end"] = INITIAL_TRAIN_END

    state["candidate_train_start"] = INITIAL_TRAIN_START
    state["candidate_train_end"] = INITIAL_TRAIN_END

    state["last_status"] = "initial_model_created"

    save_state(
        state
    )

    return True


# =========================
# BUILD NEXT ROLLING TRAIN WINDOW
# =========================
def build_rolling_train_window(check_end):
    new_train_end = pd.to_datetime(
        check_end
    )

    new_train_start = (
        new_train_end
        -
        pd.DateOffset(
            months=TRAIN_WINDOW_MONTHS
        )
    )

    return (
        to_date_string(new_train_start),
        to_date_string(new_train_end)
    )


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
        f"DEGRADATION_RATIO: {DEGRADATION_RATIO}"
    )

    log(
        f"CHECK_WINDOW_MONTHS: {CHECK_WINDOW_MONTHS}"
    )

    log(
        f"TRAIN_WINDOW_MONTHS: {TRAIN_WINDOW_MONTHS}"
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

    check_end = (
        check_start
        +
        pd.DateOffset(
            months=CHECK_WINDOW_MONTHS
        )
    )

    if check_end > drift_end_limit:
        check_end = drift_end_limit

    candidate_train_start, candidate_train_end = build_rolling_train_window(
        check_end
    )

    log(
        "\n📦 Current model train window:"
    )

    log(
        f"{state['model_train_start']} → {state['model_train_end']}"
    )

    log(
        "\n📦 Candidate train window:"
    )

    log(
        f"{candidate_train_start} → {candidate_train_end}"
    )

    log(
        "\n📦 Drift check window:"
    )

    log(
        f"{check_start} → {check_end}"
    )

    state["last_check_start"] = to_date_string(
        check_start
    )

    state["last_check_end"] = to_date_string(
        check_end
    )

    state["last_candidate_train_start"] = candidate_train_start
    state["last_candidate_train_end"] = candidate_train_end

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

        state["next_check_start"] = to_date_string(
            check_end
        )

        state["candidate_train_start"] = candidate_train_start
        state["candidate_train_end"] = candidate_train_end

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

    if hasattr(
        model,
        "feature_names_in_"
    ):
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

    baseline_mae = get_baseline_mae()

    if baseline_mae is None:
        log(
            "\n⚠️ Cannot find baseline MAE. Ratio-based drift will be disabled."
        )
    else:
        log(
            f"\n📌 Baseline MAE: {baseline_mae}"
        )

    drift, mae = detect_drift_by_mae(
        y_true=y_true,
        y_pred=y_pred,
        mae_threshold=MAE_THRESHOLD,
        model_version=model_version,
        baseline_mae=baseline_mae,
        degradation_ratio=DEGRADATION_RATIO
    )

    ratio_threshold = None

    if baseline_mae is not None:
        ratio_threshold = (
            baseline_mae
            *
            DEGRADATION_RATIO
        )

    state["last_drift"] = bool(
        drift
    )

    state["last_mae"] = float(
        mae
    )

    state["last_baseline_mae"] = (
        float(baseline_mae)
        if baseline_mae is not None
        else None
    )

    state["last_ratio_threshold"] = (
        float(ratio_threshold)
        if ratio_threshold is not None
        else None
    )

    state["candidate_train_start"] = candidate_train_start
    state["candidate_train_end"] = candidate_train_end

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

        log(
            "\n📦 Retrain with rolling train window:"
        )

        log(
            f"{candidate_train_start} → {candidate_train_end}"
        )

        state["last_status"] = "retraining"

        save_state(
            state
        )

        run_pipeline(
            train_start_date=candidate_train_start,
            train_end_date=candidate_train_end
        )

        log(
            "\n✅ Retrain complete."
        )

        state["model_train_start"] = candidate_train_start
        state["model_train_end"] = candidate_train_end

        state["last_status"] = "retrain_complete"

        print_best_model_info()

    else:
        log(
            "\n✅ No drift → keep current model."
        )

        log(
            "📌 Candidate train window updated, but model is not retrained."
        )

        state["last_status"] = "no_drift_keep_model"

    state["next_check_start"] = to_date_string(
        check_end
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