# retrain_job.py
import os
import json
import time
import joblib
import pandas as pd

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
        "100"
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
        "run_count": 0
    }


# =========================
# SAVE STATE
# =========================
def save_state(state):
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
# TRAIN INITIAL MODEL IF MISSING
# =========================
def train_initial_model_if_missing():
    if os.path.exists(
        MODEL_PATH
    ):
        return

    print(
        "\n⚠️ No model found."
    )

    print(
        "🚀 Training initial model..."
    )

    print(
        f"Train window: {INITIAL_TRAIN_START} → {INITIAL_TRAIN_END}"
    )

    run_pipeline(
        train_start_date=INITIAL_TRAIN_START,
        train_end_date=INITIAL_TRAIN_END
    )

    print(
        "✅ Initial model created."
    )


# =========================
# CHECK DRIFT ONCE
# =========================
def check_drift_once():
    ensure_folders()

    print(
        "\n=============================="
    )

    print(
        "🔍 DRIFT WORKER START CHECK"
    )

    print(
        "=============================="
    )

    print(
        f"DATA_PATH: {DATA_PATH}"
    )

    print(
        f"MODEL_PATH: {MODEL_PATH}"
    )

    print(
        f"MAE_THRESHOLD: {MAE_THRESHOLD}"
    )

    train_initial_model_if_missing()

    state = load_state()

    check_start = pd.to_datetime(
        state["next_check_start"]
    )

    drift_end_limit = pd.to_datetime(
        DRIFT_END_LIMIT
    )

    if check_start >= drift_end_limit:
        print(
            "\n✅ All drift windows have been checked."
        )

        print(
            f"Current next_check_start: {check_start}"
        )

        return

    check_end = check_start + pd.DateOffset(
        months=CHECK_WINDOW_MONTHS
    )

    if check_end > drift_end_limit:
        check_end = drift_end_limit

    print(
        "\n📦 Drift check window:"
    )

    print(
        f"{check_start} → {check_end}"
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

    print(
        "\n📊 Dataset time range:"
    )

    print(
        f"{data_min} → {data_max}"
    )

    current_window = df[
        (df["date_time"] >= check_start)
        &
        (df["date_time"] < check_end)
    ].copy()

    if len(current_window) == 0:
        print(
            "\n⚠️ No data found in this drift window."
        )

        state["next_check_start"] = str(
            check_end.date()
        )

        save_state(
            state
        )

        return

    print(
        f"\n📦 Drift check rows: {len(current_window)}"
    )

    print(
        "\n📦 Loading current model..."
    )

    model = joblib.load(
        MODEL_PATH
    )

    print(
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

    print(
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

    if drift:
        print(
            "\n🚨 Drift found → retraining model"
        )

        new_train_start = INITIAL_TRAIN_START

        new_train_end = str(
            check_end.date()
        )

        print(
            "\n📦 New train window:"
        )

        print(
            f"{new_train_start} → {new_train_end}"
        )

        run_pipeline(
            train_start_date=new_train_start,
            train_end_date=new_train_end
        )

        print(
            "\n✅ Retrain complete."
        )

    else:
        print(
            "\n✅ No drift → keep current model."
        )

    state["last_check_start"] = str(
        check_start.date()
    )

    state["last_check_end"] = str(
        check_end.date()
    )

    state["last_drift"] = bool(
        drift
    )

    state["last_mae"] = float(
        mae
    )

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

    print(
        "\n📝 Drift state saved."
    )

    print(
        f"Next check starts at: {state['next_check_start']}"
    )


# =========================
# MAIN LOOP
# =========================
if __name__ == "__main__":

    ensure_folders()

    print(
        "\n🚀 Traffic Drift Worker Started"
    )

    print(
        f"RUN_ONCE: {RUN_ONCE}"
    )

    print(
        f"CHECK_INTERVAL_SECONDS: {CHECK_INTERVAL_SECONDS}"
    )

    while True:

        try:

            check_drift_once()

        except Exception as e:

            print(
                "\n❌ Drift worker error:"
            )

            print(
                e
            )

        if RUN_ONCE:
            print(
                "\n✅ RUN_ONCE=true → worker stopped."
            )
            break

        print(
            f"\n⏳ Sleeping {CHECK_INTERVAL_SECONDS} seconds..."
        )

        time.sleep(
            CHECK_INTERVAL_SECONDS
        )