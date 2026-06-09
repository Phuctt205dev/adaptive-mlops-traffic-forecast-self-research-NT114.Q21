import json
import os
import shutil
import time
from datetime import datetime

import joblib
import pandas as pd
from sklearn.metrics import mean_absolute_error

from src.drift import detect_drift_by_mae, get_historical_mae_baseline
from src.pipeline import run_pipeline, save_model_info
from src.preprocess import load_data, preprocess


DATA_PATH = os.getenv("DATA_PATH", "data/TrafficVolumeData.csv")
MODEL_PATH = os.getenv("MODEL_PATH", "models/best_model.pkl")
MODEL_INFO_PATH = os.getenv(
    "MODEL_INFO_PATH",
    "models/best_model_info.json",
)
CANDIDATE_MODEL_PATH = os.getenv(
    "CANDIDATE_MODEL_PATH",
    "models/candidate_model.pkl",
)
CANDIDATE_INFO_PATH = os.getenv(
    "CANDIDATE_INFO_PATH",
    "models/candidate_model_info.json",
)
STATE_PATH = os.getenv("STATE_PATH", "monitoring/drift_state.json")
DRIFT_HISTORY_PATH = os.getenv(
    "DRIFT_HISTORY_PATH",
    "monitoring/drift_history.csv",
)
PROMOTION_HISTORY_PATH = os.getenv(
    "PROMOTION_HISTORY_PATH",
    "monitoring/promotion_history.csv",
)

MAE_THRESHOLD = float(os.getenv("MAE_THRESHOLD", "700"))
DEGRADATION_RATIO = float(os.getenv("DEGRADATION_RATIO", "1.2"))
BASELINE_HISTORY_WINDOWS = int(
    os.getenv("BASELINE_HISTORY_WINDOWS", "6")
)
MIN_BASELINE_WINDOWS = int(os.getenv("MIN_BASELINE_WINDOWS", "3"))
MIN_PROMOTION_IMPROVEMENT = float(
    os.getenv("MIN_PROMOTION_IMPROVEMENT", "0.05")
)

CHECK_INTERVAL_SECONDS = int(
    os.getenv("CHECK_INTERVAL_SECONDS", "60")
)
CHECK_WINDOW_MONTHS = int(os.getenv("CHECK_WINDOW_MONTHS", "1"))
TRAIN_WINDOW_MONTHS = int(os.getenv("TRAIN_WINDOW_MONTHS", "12"))

DRIFT_START_DATE = os.getenv("DRIFT_START_DATE", "2013-04-01")
DRIFT_END_LIMIT = os.getenv("DRIFT_END_LIMIT", "2014-09-01")
INITIAL_TRAIN_START = os.getenv("INITIAL_TRAIN_START", "2013-01-01")
INITIAL_TRAIN_END = os.getenv("INITIAL_TRAIN_END", "2013-04-01")
RUN_ONCE = os.getenv("RUN_ONCE", "false").lower() == "true"


def log(message):
    print(message, flush=True)


def to_date_string(value):
    return str(pd.to_datetime(value).date())


def ensure_folders():
    for folder in [
        "monitoring",
        "models",
        "mlruns",
        "data_versions",
        "results",
    ]:
        os.makedirs(folder, exist_ok=True)


def get_default_state():
    return {
        "next_check_start": DRIFT_START_DATE,
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
        "last_promotion_decision": None,
        "last_champion_mae": None,
        "last_candidate_mae": None,
        "last_improvement_ratio": None,
        "last_status": "initialized",
        "last_error": None,
        "run_count": 0,
        "updated_at": datetime.now().isoformat(),
    }


def load_state():
    state = get_default_state()
    if not os.path.exists(STATE_PATH):
        return state

    try:
        with open(STATE_PATH, "r", encoding="utf-8") as file:
            saved_state = json.load(file)
        state.update(saved_state)
    except (OSError, json.JSONDecodeError):
        log("Cannot read drift state. Default state will be used.")

    return state


def save_state(state):
    state["updated_at"] = datetime.now().isoformat()
    state_directory = os.path.dirname(STATE_PATH)
    if state_directory:
        os.makedirs(state_directory, exist_ok=True)

    with open(STATE_PATH, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=4, ensure_ascii=False)


def load_json(path):
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return None


def append_csv_row(path, row):
    if os.path.exists(path):
        history = pd.read_csv(path)
    else:
        history = pd.DataFrame()

    history = pd.concat(
        [history, pd.DataFrame([row])],
        ignore_index=True,
    )
    history.to_csv(path, index=False)


def get_champion_info():
    return load_json(MODEL_INFO_PATH)


def get_champion_version():
    info = get_champion_info() or {}
    return str(info.get("model_version", "unknown"))


def candidate_exists():
    return (
        os.path.exists(CANDIDATE_MODEL_PATH)
        and os.path.exists(CANDIDATE_INFO_PATH)
    )


def align_features(model, dataframe):
    features = dataframe.drop(
        ["traffic_volume", "date_time"],
        axis=1,
    )
    if hasattr(model, "feature_names_in_"):
        features = features.reindex(
            columns=model.feature_names_in_,
            fill_value=0,
        )
    return features


def evaluate_model(model, dataframe):
    features = align_features(model, dataframe)
    predictions = model.predict(features)
    actual = dataframe["traffic_volume"]
    mae = mean_absolute_error(actual, predictions)
    return float(mae), predictions


def calculate_improvement_ratio(champion_mae, candidate_mae):
    if champion_mae <= 0:
        return 0.0
    return (champion_mae - candidate_mae) / champion_mae


def should_promote_candidate(
    champion_mae,
    candidate_mae,
    minimum_improvement=MIN_PROMOTION_IMPROVEMENT,
):
    improvement = calculate_improvement_ratio(
        champion_mae,
        candidate_mae,
    )
    return improvement >= minimum_improvement, improvement


def remove_pending_candidate():
    for path in [CANDIDATE_MODEL_PATH, CANDIDATE_INFO_PATH]:
        if os.path.exists(path):
            os.remove(path)


def promote_candidate(candidate_info, promotion_metrics):
    """
    Replace the champion only after an out-of-sample promotion comparison.

    The versioned model created by the pipeline remains available for rollback.
    """
    temporary_model_path = f"{MODEL_PATH}.tmp"
    shutil.copy2(CANDIDATE_MODEL_PATH, temporary_model_path)
    os.replace(temporary_model_path, MODEL_PATH)

    promoted_info = dict(candidate_info)
    promoted_info.update(
        {
            "model_role": "champion",
            "model_file": MODEL_PATH,
            "promoted_at": datetime.now().isoformat(),
            "promotion_metrics": promotion_metrics,
        }
    )
    save_model_info(promoted_info, MODEL_INFO_PATH)
    remove_pending_candidate()
    return promoted_info


def evaluate_pending_candidate(champion_model, evaluation_window):
    """
    Compare champion and candidate on the same unseen time window.

    Candidate training ends before this window starts because the candidate was
    created during the previous worker iteration.
    """
    if not candidate_exists():
        champion_mae, champion_predictions = evaluate_model(
            champion_model,
            evaluation_window,
        )
        return {
            "model": champion_model,
            "predictions": champion_predictions,
            "promoted": False,
            "decision": "no_candidate",
            "champion_mae": champion_mae,
            "candidate_mae": None,
            "improvement_ratio": None,
        }

    candidate_model = joblib.load(CANDIDATE_MODEL_PATH)
    candidate_info = load_json(CANDIDATE_INFO_PATH)
    if candidate_info is None:
        remove_pending_candidate()
        champion_mae, champion_predictions = evaluate_model(
            champion_model,
            evaluation_window,
        )
        log("Candidate metadata is invalid. Candidate was discarded.")
        return {
            "model": champion_model,
            "predictions": champion_predictions,
            "promoted": False,
            "decision": "invalid_candidate",
            "champion_mae": champion_mae,
            "candidate_mae": None,
            "improvement_ratio": None,
        }

    champion_info = get_champion_info() or {}

    champion_mae, champion_predictions = evaluate_model(
        champion_model,
        evaluation_window,
    )
    candidate_mae, candidate_predictions = evaluate_model(
        candidate_model,
        evaluation_window,
    )
    promote, improvement = should_promote_candidate(
        champion_mae,
        candidate_mae,
    )

    decision = "promoted" if promote else "rejected"
    promotion_row = {
        "timestamp": datetime.now().isoformat(),
        "evaluation_start": to_date_string(
            evaluation_window["date_time"].min()
        ),
        "evaluation_end": to_date_string(
            evaluation_window["date_time"].max()
        ),
        "champion_version": champion_info.get(
            "model_version",
            "unknown",
        ),
        "candidate_version": candidate_info.get(
            "model_version",
            "unknown",
        ),
        "champion_mae": round(champion_mae, 4),
        "candidate_mae": round(candidate_mae, 4),
        "improvement_ratio": round(improvement, 6),
        "minimum_improvement": MIN_PROMOTION_IMPROVEMENT,
        "decision": decision,
    }
    append_csv_row(PROMOTION_HISTORY_PATH, promotion_row)

    log("\nChampion-Challenger comparison")
    log(f"Champion MAE: {champion_mae:.2f}")
    log(f"Candidate MAE: {candidate_mae:.2f}")
    log(f"Improvement: {improvement * 100:.2f}%")

    if promote:
        promoted_info = promote_candidate(candidate_info, promotion_row)
        log(
            "Candidate promoted to champion: "
            f"{promoted_info.get('model_version')}"
        )
        return {
            "model": candidate_model,
            "predictions": candidate_predictions,
            "promoted": True,
            "decision": decision,
            "champion_mae": champion_mae,
            "candidate_mae": candidate_mae,
            "improvement_ratio": improvement,
            "promoted_info": promoted_info,
        }

    remove_pending_candidate()
    log("Candidate rejected. Current champion is kept.")
    return {
        "model": champion_model,
        "predictions": champion_predictions,
        "promoted": False,
        "decision": decision,
        "champion_mae": champion_mae,
        "candidate_mae": candidate_mae,
        "improvement_ratio": improvement,
    }


def train_initial_model_if_missing():
    if os.path.exists(MODEL_PATH):
        return

    log("\nNo champion model found. Training the initial model...")
    run_pipeline(
        train_start_date=INITIAL_TRAIN_START,
        train_end_date=INITIAL_TRAIN_END,
        output_model_path=MODEL_PATH,
        output_info_path=MODEL_INFO_PATH,
        model_role="champion",
    )

    state = load_state()
    state["model_train_start"] = INITIAL_TRAIN_START
    state["model_train_end"] = INITIAL_TRAIN_END
    state["last_status"] = "initial_model_created"
    save_state(state)


def build_rolling_train_window(check_end):
    train_end = pd.to_datetime(check_end)
    train_start = train_end - pd.DateOffset(months=TRAIN_WINDOW_MONTHS)
    return to_date_string(train_start), to_date_string(train_end)


def train_candidate(train_start, train_end):
    log(f"\nTraining candidate on {train_start} -> {train_end}")
    return run_pipeline(
        train_start_date=train_start,
        train_end_date=train_end,
        output_model_path=CANDIDATE_MODEL_PATH,
        output_info_path=CANDIDATE_INFO_PATH,
        model_role="candidate",
    )


def check_drift_once():
    ensure_folders()
    train_initial_model_if_missing()

    state = load_state()
    check_start = pd.to_datetime(state["next_check_start"])
    drift_end_limit = pd.to_datetime(DRIFT_END_LIMIT)

    if check_start >= drift_end_limit:
        state["last_status"] = "finished_all_windows"
        save_state(state)
        log("All drift windows have been checked.")
        return

    check_end = check_start + pd.DateOffset(
        months=CHECK_WINDOW_MONTHS
    )
    check_end = min(check_end, drift_end_limit)

    state.update(
        {
            "last_check_start": to_date_string(check_start),
            "last_check_end": to_date_string(check_end),
            "last_status": "checking_drift",
            "last_error": None,
        }
    )
    save_state(state)

    dataframe = preprocess(load_data(DATA_PATH)).sort_values("date_time")
    current_window = dataframe[
        (dataframe["date_time"] >= check_start)
        & (dataframe["date_time"] < check_end)
    ].copy()

    if current_window.empty:
        state["next_check_start"] = to_date_string(check_end)
        state["last_status"] = "no_data"
        state["run_count"] = int(state.get("run_count", 0)) + 1
        save_state(state)
        log("No data found in the current drift window.")
        return

    log(
        f"\nChecking {len(current_window)} rows: "
        f"{to_date_string(check_start)} -> {to_date_string(check_end)}"
    )

    champion_model = joblib.load(MODEL_PATH)
    comparison = evaluate_pending_candidate(
        champion_model,
        current_window,
    )

    active_model = comparison["model"]
    active_predictions = comparison["predictions"]
    active_version = get_champion_version()

    # The baseline only uses earlier production windows of this exact champion.
    baseline_mae = get_historical_mae_baseline(
        model_version=active_version,
        history_size=BASELINE_HISTORY_WINDOWS,
        minimum_windows=MIN_BASELINE_WINDOWS,
        log_path=DRIFT_HISTORY_PATH,
    )

    drift, current_mae = detect_drift_by_mae(
        y_true=current_window["traffic_volume"],
        y_pred=active_predictions,
        mae_threshold=MAE_THRESHOLD,
        model_version=active_version,
        baseline_mae=baseline_mae,
        degradation_ratio=DEGRADATION_RATIO,
        log_path=DRIFT_HISTORY_PATH,
    )

    ratio_threshold = (
        baseline_mae * DEGRADATION_RATIO
        if baseline_mae is not None
        else None
    )
    state.update(
        {
            "last_drift": bool(drift),
            "last_mae": float(current_mae),
            "last_baseline_mae": baseline_mae,
            "last_ratio_threshold": ratio_threshold,
            "last_promotion_decision": comparison["decision"],
            "last_champion_mae": comparison["champion_mae"],
            "last_candidate_mae": comparison["candidate_mae"],
            "last_improvement_ratio": comparison["improvement_ratio"],
        }
    )

    if comparison["promoted"]:
        promoted_info = comparison["promoted_info"]
        state["model_train_start"] = promoted_info["train_start_date"]
        state["model_train_end"] = promoted_info["train_end_date"]

    if drift:
        candidate_train_start, candidate_train_end = (
            build_rolling_train_window(check_end)
        )
        state["last_status"] = "training_candidate"
        state["last_candidate_train_start"] = candidate_train_start
        state["last_candidate_train_end"] = candidate_train_end
        save_state(state)

        train_candidate(candidate_train_start, candidate_train_end)
        state["last_status"] = "candidate_waiting_for_promotion"
    else:
        state["last_status"] = "no_drift_keep_champion"

    state["next_check_start"] = to_date_string(check_end)
    state["run_count"] = int(state.get("run_count", 0)) + 1
    save_state(state)

    log(f"Current MAE: {current_mae:.2f}")
    log(f"Next check starts at: {state['next_check_start']}")


if __name__ == "__main__":
    ensure_folders()
    log("Traffic drift worker started.")

    while True:
        try:
            check_drift_once()
        except Exception as error:
            log(f"Drift worker error: {error}")
            state = load_state()
            state["last_status"] = "error"
            state["last_error"] = str(error)
            save_state(state)

        if RUN_ONCE:
            log("RUN_ONCE=true. Worker stopped.")
            break

        log(f"Sleeping {CHECK_INTERVAL_SECONDS} seconds...")
        time.sleep(CHECK_INTERVAL_SECONDS)
