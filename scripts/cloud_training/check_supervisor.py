"""Explicit isolated Kaggle supervisor checks; synthetic data only, no DB/S3."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid
import zipfile
import csv
from datetime import datetime, timedelta
import math

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.cloud_training.kaggle_execution import KaggleExecution, TrainingCancelled, initial_ledger
from src.cloud_training.kaggle_transport import SDKTransport
from src.cloud_training.package import configuration_digest, prepare_package
from src.cloud_training.probe import sha256_file, write_json


SYNTHETIC_CHILD = '''import json,sys,time
from pathlib import Path
from sklearn.ensemble import RandomForestRegressor
import numpy as np
job=json.loads(Path(sys.argv[sys.argv.index("--job")+1]).read_text())
event={"context":job["context"],"event":{"sequence":1,"model":"random_forest_lag","stage":"model_started"}}
print("TRAFFIC_EXECUTION_EVENT "+json.dumps(event),flush=True)
rng=np.random.default_rng(42);x=rng.normal(size=(1000,8));y=x.sum(axis=1)
deadline=time.monotonic()+300
while time.monotonic()<deadline:
 RandomForestRegressor(n_estimators=20,max_depth=8,n_jobs=1,random_state=42).fit(x,y)
raise SystemExit(2)
'''

GPU_CHILD = '''import json,sys,time
from pathlib import Path
import numpy as np
import tensorflow as tf
job=json.loads(Path(sys.argv[sys.argv.index("--job")+1]).read_text())
devices=tf.config.list_physical_devices("GPU")
if not devices:raise RuntimeError("GPU required")
for device in devices:tf.config.experimental.set_memory_growth(device,True)
with tf.device("/GPU:0"):
 probe=tf.constant([1.0,2.0])
 if "GPU" not in probe.device:raise RuntimeError("GPU placement missing")
 model=tf.keras.Sequential([tf.keras.layers.Input((8,)),tf.keras.layers.Dense(4,activation="relu"),tf.keras.layers.Dense(1)])
 model.compile(optimizer="adam",loss="mse")
 rng=np.random.default_rng(42);x=rng.normal(size=(256,8)).astype("float32");y=x.sum(axis=1)
 deadline=time.monotonic()+300;seq=0
 while time.monotonic()<deadline:
  history=model.fit(x,y,batch_size=32,epochs=1,verbose=0)
  seq+=1
  event={"context":job["context"],"event":{"sequence":seq,"model":"lstm","stage":"epoch_completed","epoch":seq,"epochs":100000,"loss":float(history.history["loss"][-1])}}
  print("TRAFFIC_EXECUTION_EVENT "+json.dumps(event),flush=True)
raise SystemExit(2)
'''


def prepare(username, purpose):
    attempt, run = str(uuid.uuid4()), str(uuid.uuid4())
    directory = ROOT / "results/kaggle-poc/integration" / attempt
    directory.mkdir(parents=True, exist_ok=False)
    dataset = directory / "synthetic.csv"
    dataset.write_bytes(b"synthetic_a,synthetic_b\n1,2\n")
    # A skipped tree checks transport/supervision without fitting a business
    # model. Cancel checks use a separately labelled tiny native-fit child.
    config = {"selected_models": ["lstm"], "training_backend": "kaggle", "_execution_protocol": 2}
    runtime_profile = None
    if purpose in {"tree-model", "tree-suite", "lstm-model", "gru-model"}:
        with dataset.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["date_time", "is_holiday", "weather_type", "weather_description",
                "air_pollution_index", "temperature", "humidity", "wind_speed", "wind_direction", "visibility_in_miles",
                "dew_point", "rain_p_h", "snow_p_h", "clouds_all", "traffic_volume"])
            writer.writeheader()
            for hour in range(1500):
                writer.writerow({"date_time": (datetime(2026, 1, 1) + timedelta(hours=hour)).isoformat(),
                    "is_holiday": "", "weather_type": "Clear" if hour % 3 else "Rain", "weather_description": "synthetic",
                    "temperature": 290 + 5 * math.sin(hour / 24), "humidity": 60, "wind_speed": 2,
                    "air_pollution_index": 30, "wind_direction": 180, "visibility_in_miles": 10, "dew_point": 15,
                    "rain_p_h": int(hour % 3 == 0), "snow_p_h": 0, "clouds_all": 20,
                    "traffic_volume": round(1000 + 200 * math.sin(hour / 12) + hour % 40, 3)})
        config.update(selected_models=["random_forest_lag"], train_start_date="2026-01-01", train_end_date="2026-02-15",
                      cv_splits=3, random_state=42, shap={"enabled": False})
        if purpose == "tree-suite":
            config["selected_models"] = ["random_forest_lag", "xgboost_lag", "lightgbm_lag"]
            config["shap"] = {"enabled": True, "max_samples": 4, "background_samples": 4, "max_seconds": 30}
        elif purpose in {"lstm-model", "gru-model"}:
            config.update(selected_models=[purpose.split("-")[0]], recurrent_sequence_length=24, recurrent_epochs=1,
                          recurrent_batch_size=64, final_test_ratio=.15)
        runtime = json.loads((ROOT / "results/cloud-training/snapshot-20261028-v2.json").read_text())
        packages = runtime["runtime"]["traffic-training-tree"]["packages"]
        runtime_profile = {name: packages[name] for name in ("scikit-learn", "joblib", "pandas")}
        if purpose == "tree-suite":
            runtime_profile.update({name: packages[name] for name in ("xgboost", "lightgbm")})
            runtime_profile["shap"] = "0.50.0"
    context = {"run_id": run, "region_id": str(uuid.uuid4()), "dataset_id": str(uuid.uuid4()),
               "attempt_id": attempt, "branch": ("lstm" if purpose == "gpu-cancel" else purpose.split("-")[0])
               if purpose in {"gpu-cancel", "lstm-model", "gru-model"} else "tree", "configuration_sha256": configuration_digest(config)}
    snapshot = prepare_package(ROOT, dataset, config, context, directory / "input", sha256_file(dataset), runtime_profile)
    if purpose in {"cancel", "gpu-cancel"}:
        archive = directory / "input/source.bin"
        with zipfile.ZipFile(archive) as source:
            entries = {item.filename: source.read(item) for item in source.infolist()}
        entries["scripts/cloud_training/execute_branch.py"] = (GPU_CHILD if purpose == "gpu-cancel" else SYNTHETIC_CHILD).encode()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for name, data in entries.items():
                target.writestr(name, data)
        snapshot["files"]["source.bin"] = {"sha256": sha256_file(archive), "size": archive.stat().st_size}
    ledger = initial_ledger(snapshot, username)
    write_json(directory / "ledger.json", ledger)
    write_json(directory / "check.json", {"purpose": purpose, "username": username, "synthetic_only": True})
    return directory


def run(directory):
    directory = Path(directory).resolve(strict=True)
    directory.relative_to((ROOT / "results/kaggle-poc/integration").resolve())
    check = json.loads((directory / "check.json").read_text())
    ledger = json.loads((directory / "ledger.json").read_text())
    sdk = ROOT / "results/kaggle-poc/venv/Scripts/python.exe"
    transport = SDKTransport(check["username"], python=sdk, repository=ROOT)
    provider = KaggleExecution(transport, directory, ledger, lambda value: write_json(directory / "ledger.json", value))
    proof = {"synthetic_only": True, "purpose": check["purpose"], "production_ready": False}
    started = time.monotonic()
    def cancelled():
        return (check["purpose"] in {"cancel", "gpu-cancel"} and provider.ledger.get("child_running_observed")
                and provider.last_sequence > 0 and time.monotonic() - started > 20)
    try:
        def progress(event):
            if event.get("sequence", 0) <= 3 or event.get("sequence", 0) % 10 == 0:
                print(json.dumps({"progress": event}), flush=True)
        result = provider.run(progress, cancelled)
        proof.update(completed=True, result_status=result.get("status"))
        if check["purpose"] in {"tree-model", "tree-suite", "lstm-model", "gru-model"}:
            write_json(directory / "result.json", result)
            proof["serving_cpu_reload_pending"] = True
    except TrainingCancelled:
        deadline = time.monotonic() + 240
        while not provider.stop() and time.monotonic() < deadline:
            provider.drain(lambda event: None)
            time.sleep(3)
        for _ in range(10):
            provider.drain(lambda event: None)
            if provider.ledger.get("cancelled_observed"):
                break
            time.sleep(1)
        proof.update(cancel_confirmed=bool(provider.ledger.get("remote_stopped")
            and provider.ledger.get("child_running_observed") and provider.ledger.get("cancelled_observed")))
        if check["purpose"] == "gpu-cancel":
            proof["gpu_epoch_observed_before_cancel"] = provider.last_sequence > 0
    except Exception:
        try:
            proof["remote_stopped_after_failure"] = provider.stop()
        except Exception:
            proof["remote_stop_unconfirmed"] = True
        raise
    finally:
        if provider.stream:
            provider.stream.stop()
            provider.stream = None
        if provider.ledger.get("remote_stopped"):
            proof["private_staging_cleaned"] = provider.cleanup()
        proof["reference"] = provider.reference if provider.ledger.get("version") else provider.ledger["reference"]
        proof["confirmed_control_writes"] = provider.ledger.get("confirmed_control_writes", 0)
        write_json(directory / "proof.json", proof)
    print(json.dumps(proof), flush=True)
    if not (proof.get("completed") or proof.get("cancel_confirmed")) or not proof.get("private_staging_cleaned"):
        raise RuntimeError("The isolated supervisor check did not pass.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "stop"])
    parser.add_argument("--username", default="truongphucdev")
    parser.add_argument("--purpose", choices=["complete", "cancel", "gpu-cancel", "tree-model", "tree-suite", "lstm-model", "gru-model"], default="complete")
    parser.add_argument("--directory")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            print(json.dumps({"prepared": str(prepare(args.username, args.purpose)), "submitted": False}))
        elif args.action == "run":
            run(args.directory)
        else:
            directory = Path(args.directory).resolve(strict=True)
            ledger = json.loads((directory / "ledger.json").read_text())
            provider = KaggleExecution(SDKTransport(args.username,
                python=ROOT / "results/kaggle-poc/venv/Scripts/python.exe", repository=ROOT), directory,
                ledger, lambda value: write_json(directory / "ledger.json", value))
            print(json.dumps({"remote_stopped": provider.stop()}))
        return 0
    except Exception as error:
        print(json.dumps({"check_failed": True, "error_type": type(error).__name__, "raw_details_omitted": True}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
