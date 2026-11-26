"""E2: what the Model Quality tab shows before/after, on the E1 offline predictions.

The real backend function `_build_report` is called; only `_predict_points` is
replaced by the saved E1 final-test predictions (no model is loaded or trained).
Usage: python -X utf8 experiments/scripts/e2_backend_replay.py
Writes experiments/outputs/e2/summary.json.
"""
import json
import statistics
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.app.services import model_quality as quality  # noqa: E402

E1 = ROOT / "experiments" / "outputs" / "e1"
CASES = [
    # name, E1 output folder, model, dataset type, region timezone used for the replay
    ("I-94 Minneapolis", "i94_full", "RandomForest", "thật"),
    ("Glasgow GD1351_R", "glasgow_GD1351_R", "LSTM", "thật"),
    ("Hồ Chí Minh", "hcm_verify_f93d0351", "RandomForest", "synthetic"),
]
WEEK = 168


def replay(frame, predictions, train_end, start, hours):
    """Run the backend report for `hours` consecutive hours from `start`; train_end = test start."""
    region = SimpleNamespace(id=uuid.uuid4(), timezone="UTC", is_active=True)
    model = SimpleNamespace(id=uuid.uuid4(), version="e1", variant="offline", artifact_uri="none")
    training = SimpleNamespace(configuration_json={"train_end_date": str(train_end)})
    dataset = SimpleNamespace(id=uuid.uuid4(), original_filename="e1.csv", sha256="0" * 64)
    with patch.object(quality, "_predict_points", return_value=predictions):
        return quality._build_report(region, model, training, dataset, frame, "reference", str(start), hours,
                                     window="replay")


def summarise(report):
    metrics, points = report["metrics"], report["points"]
    paired = [p for p in points if p["absolute_error"] is not None]
    low = [p for p in paired if p["low_traffic"]]
    normal = [p for p in paired if not p["low_traffic"]]
    pct = lambda rows: [p["percent_error"] for p in rows if p["percent_error"] is not None]
    total_error = sum(p["absolute_error"] for p in paired)
    return {
        "paired_hours": len(paired), "mae": metrics["mae"], "rmse": metrics["rmse"],
        "wape_percent": metrics["wape_percent"], "actual_mean": metrics["actual_mean"],
        "low_traffic_below": metrics["low_traffic_below"],
        "baseline": metrics["baseline"],
        "low_traffic_hours": len(low),
        "low_traffic_share_of_error_percent": sum(p["absolute_error"] for p in low) / total_error * 100 if total_error else None,
        "median_percent_error_low": statistics.median(pct(low)) if pct(low) else None,
        "median_percent_error_normal": statistics.median(pct(normal)) if pct(normal) else None,
    }


def main():
    results = {}
    for name, folder, model_name, kind in CASES:
        summary = json.loads((E1 / folder / "summary.json").read_text(encoding="utf-8"))
        frame = pd.read_csv(ROOT / summary["data_path"].replace("\\", "/"))
        predicted = pd.read_csv(E1 / folder / f"{model_name}_final_test_predictions.csv", parse_dates=["date_time"])
        predictions = {ts.to_pydatetime(): float(v) for ts, v in zip(predicted.date_time, predicted.prediction)}
        start, end = predicted.date_time.min(), predicted.date_time.max()
        hours = int((end - start) / pd.Timedelta(hours=1)) + 1
        whole = summarise(replay(frame, predictions, start, start, hours))

        weeks = []
        for week_start in pd.date_range(start, end - pd.Timedelta(hours=WEEK - 1), freq=f"{WEEK}h"):
            report = replay(frame, predictions, start, week_start, WEEK)
            metrics = report["metrics"]
            if metrics["wape_percent"] is None or report["coverage"]["paired_points"] < WEEK // 2:
                continue  # Skip weeks with less than half the hours paired.
            weeks.append({"start": str(week_start), "paired_hours": report["coverage"]["paired_points"],
                          "mae": metrics["mae"], "wape_percent": metrics["wape_percent"],
                          "relative_change_percent": metrics["baseline"]["relative_change_percent"]})
        changes = [w["relative_change_percent"] for w in weeks if w["relative_change_percent"] is not None]
        wapes = [w["wape_percent"] for w in weeks]
        maes = [w["mae"] for w in weeks]
        results[name] = {
            "dataset_type": kind, "model": model_name, "test_start": str(start), "test_end": str(end),
            "whole_test": whole,
            "weekly": {"weeks": len(weeks),
                       "mae_min_median_max": [min(maes), statistics.median(maes), max(maes)],
                       "wape_min_median_max": [min(wapes), statistics.median(wapes), max(wapes)],
                       "weeks_worse_than_last_week": sum(c > 0 for c in changes),
                       "weeks_with_baseline": len(changes),
                       "change_min_median_max": [min(changes), statistics.median(changes), max(changes)] if changes else None},
            "weeks": weeks,
        }
        w = results[name]
        print(f"{name} ({kind}, {model_name}) {start:%Y-%m-%d}..{end:%Y-%m-%d}: paired {whole['paired_hours']} h")
        print(f"  before: Average difference {whole['mae']:.1f} vehicles/hour")
        print(f"  after : Average error {whole['wape_percent']:.1f}% ({whole['mae']:.1f} vehicles/hour); "
              f"vs same hour last week {whole['baseline']['relative_change_percent']:+.1f}% "
              f"on {whole['baseline']['paired_points']} h (last week MAE {whole['baseline']['mae']:.1f})")
        print(f"  low traffic (< {whole['low_traffic_below']:.0f}): {whole['low_traffic_hours']} h, "
              f"{whole['low_traffic_share_of_error_percent']:.1f}% of total error; median % error "
              f"{whole['median_percent_error_low']:.1f}% vs {whole['median_percent_error_normal']:.1f}%")
        print(f"  weekly ({w['weekly']['weeks']} weeks): WAPE {w['weekly']['wape_min_median_max']}, "
              f"worse than last week {w['weekly']['weeks_worse_than_last_week']}/{w['weekly']['weeks_with_baseline']}")
    out = ROOT / "experiments" / "outputs" / "e2"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    print("wrote", out / "summary.json")


if __name__ == "__main__":
    main()
