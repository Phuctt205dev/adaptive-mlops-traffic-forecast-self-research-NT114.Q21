"""E4 step 6.2 — flatten the CI/CD `delivery-metrics` artifacts into one table.

Input: results/e4/delivery/<run>-<attempt>/delivery-metrics.json (downloaded read-only from GitHub; ignored by Git).
Output: experiments/outputs/e4/delivery_runs.csv (no credentials: run id, commit, timings, outcome only).
Usage: python -X utf8 experiments/scripts/e4_delivery_table.py
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "results/e4/delivery"
OUT = ROOT / "experiments/outputs/e4/delivery_runs.csv"
PHASES = ("plan", "admission", "stage_api", "stage_workers", "update_airflow", "promote", "verify_promoted",
          "retire", "release_admission", "rollback")
SECONDS = ("ci_wall", "ci_execution_span", "initial_wait", "ci_runner", "backend_commit_to_verify",
           "pages_commit_to_publish", "failed_release_to_verified_rollback")


def main():
    rows = []
    for path in sorted(SOURCE.glob("*/delivery-metrics.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        row = {"run_id": d["run_id"], "attempt": d["attempt"], "event": d["event"], "commit": d["commit"][:7],
               "created_at": d["timestamps"].get("run_created_at"), "scenario": d["context"].get("scenario"),
               "ci_accepted": d.get("ci_accepted"), "timing_eligible": d.get("timing_sample_eligible"),
               "backend": d.get("backend"), "pages_published": d.get("pages_published")}
        row.update({key: d["seconds"].get(key) for key in SECONDS})
        phases = {p["phase"]: p["seconds"] for p in d.get("phases") or []}
        row.update({"phase_" + name: phases.get(name) for name in PHASES})
        rows.append(row)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print("rows", len(rows), "->", OUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
