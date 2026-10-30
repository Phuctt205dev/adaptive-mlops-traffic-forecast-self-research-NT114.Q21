"""Offline prepare; run requires approval for a tiny synthetic GPU fit, no app data."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.cloud_training.package import configuration_digest, prepare_package
from src.cloud_training.probe import sha256_file
from src.cloud_training.kaggle_execution import KaggleExecution, initial_ledger
from src.cloud_training.kaggle_transport import SDKTransport

CHILD = r'''
import json,os,sys,traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
job=json.loads(Path(sys.argv[sys.argv.index("--job")+1]).read_text())
output=Path(sys.argv[sys.argv.index("--output")+1]);output.mkdir(parents=True,exist_ok=False)
from scripts.cloud_training.original_execute_branch import prepare_runtime
prepare_runtime(job)
import numpy as np,pandas as pd,tensorflow as tf
from tensorflow import keras
from sklearn.preprocessing import StandardScaler
from src import time_series_cross_validation as cv
from src.models import recurrent
from src.training_progress import reporting
from src.cloud_training.artifacts import write_bundle
from src.time_series_preprocess import ORIGINAL_COLUMN_ORDER,prepare_hourly_time_series
if not tf.config.list_physical_devices("GPU"):raise RuntimeError("GPU required")
results=[]
for name in ("LSTM","GRU"):
 rng=np.random.default_rng(42)
 events=[];row={}
 result={"model":name,"passed":False}
 try:
  raw=pd.DataFrame({column:np.arange(80,dtype=float) for column in ORIGINAL_COLUMN_ORDER})
  raw["date_time"]=pd.date_range("2026-01-01",periods=80,freq="h")
  raw["is_holiday"]=None;raw["weather_type"]="Clear";raw["weather_description"]="clear sky"
  raw["temperature"]=rng.normal(size=80);raw["humidity"]=rng.normal(size=80);raw["rain_p_h"]=np.abs(rng.normal(size=80))
  raw["traffic_volume"]=100+10*rng.normal(size=80)
  hourly,audit,_=prepare_hourly_time_series(raw)
  start,end=hourly.date_time.iloc[24],hourly.date_time.iloc[63]
  source=cv._prepare_neural_sequence_source(hourly,audit,start,end,6)
  data,pre=cv._build_neural_fold_datasets(source,hourly.date_time.iloc[24:56],hourly.date_time.iloc[56:64],6,
    selected_groups=["temperature","humidity","rain_p_h"])
  scaler=pre["target_scaler"];names=pre["sequence_feature_names"]
  assert data["train"]["X"].shape==(32,6,3) and data["validation"]["X"].shape==(8,6,3)
  result["history_context_preserved"]=bool(source.date_time.min()<start)
  result["training_targets_in_window"]=bool(pd.Timestamp(data["train"]["timestamps"][0])>=start)
  with reporting(events.append):
   metrics,epoch,trained,seconds=cv._train_neural_fold(name,data,scaler,1,8,42,0,
     selection_config={"enabled":True,"retain_share":.5},selection_row=row,
     feature_names=names,
     shap_config={"max_samples":2,"background_samples":2,"batch_size":1,"nsamples":8,"max_seconds":60})
  result.update(passed=True,metrics=metrics,epoch=epoch,trained_epochs=trained,selection=row,
    callback_identity_valid=all(isinstance(c,keras.callbacks.Callback) for c in recurrent.build_training_callbacks()))
 except Exception as error:
  frames=traceback.extract_tb(error.__traceback__)
  project=[f for f in frames if "/src/" in f.filename.replace("\\","/")]
  frame=project[-1] if project else frames[-1]
  result.update(error_type=type(error).__name__,source=Path(frame.filename).name,line=frame.lineno,
                final_source=Path(frames[-1].filename).name,final_line=frames[-1].lineno)
 result["stages"]=[e["stage"] for e in events]
 results.append(result)
 keras.backend.clear_session()
write_bundle({"branch":"lstm","status":"completed","candidates":[],"probe_results":results,
 "fit_called":True,"synthetic_only":True,"runtime":{"tensorflow":tf.__version__,"keras":keras.__version__}},job["context"],output)
'''


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def prepare(username, child_source=None):
    directory = ROOT / "results/shap-retrain/neural-fold-probe" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    dataset = directory / "synthetic.csv"
    dataset.write_bytes(b"synthetic_only\n1\n")
    configuration = {"selected_models": ["lstm", "gru"], "training_backend": "kaggle", "shap": {"enabled": True}}
    context = {name: str(uuid.uuid4()) for name in ("run_id", "region_id", "dataset_id", "attempt_id")}
    context.update(branch="lstm", configuration_sha256=configuration_digest(configuration))
    snapshot = prepare_package(ROOT, dataset, configuration, context, directory / "input", sha256_file(dataset), {"shap": "0.50.0"})
    archive = directory / "input/source.bin"
    with zipfile.ZipFile(archive) as source:
        entries = {item.filename: source.read(item) for item in source.infolist()}
    entries["scripts/cloud_training/original_execute_branch.py"] = entries["scripts/cloud_training/execute_branch.py"]
    entries["scripts/cloud_training/execute_branch.py"] = (child_source or CHILD).encode()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as destination:
        for name, data in entries.items():
            destination.writestr(name, data)
    snapshot["files"]["source.bin"] = {"sha256": sha256_file(archive), "size": archive.stat().st_size}
    write(directory / "ledger.json", initial_ledger(snapshot, username))
    check = {"username": username, "synthetic_only": True, "samples": 40, "sequence_length": 6,
             "features": 3, "max_epochs": 1, "fit_requires_user_approval": True,
             "source_sha256": {name: sha256_file(ROOT / name) for name in (
                 "src/time_series_cross_validation.py", "src/feature_selection.py", "src/models/recurrent.py")}}
    write(directory / "check.json", check)
    return {"directory": str(directory), "submitted": False, "source_archive_bytes": archive.stat().st_size,
            "synthetic_csv_bytes": dataset.stat().st_size, "samples": 40, "sequence_length": 6,
            "features": 3, "max_epochs": 1, "models": ["LSTM", "GRU"]}


def run(directory, *, stop=False):
    directory = Path(directory).resolve(strict=True)
    directory.relative_to((ROOT / "results/shap-retrain/neural-fold-probe").resolve())
    check = json.loads((directory / "check.json").read_text())
    if not stop:
        for name, expected in check["source_sha256"].items():
            if sha256_file(ROOT / name) != expected:
                raise RuntimeError("Probe source changed; prepare a fresh package.")
    provider = KaggleExecution(SDKTransport(check["username"], repository=ROOT,
        python=ROOT / "results/kaggle-poc/venv/Scripts/python.exe"), directory,
        json.loads((directory / "ledger.json").read_text()), lambda value: write(directory / "ledger.json", value))
    proof = {"synthetic_only": True, "fit_called": not stop, "passed": False}
    try:
        if not stop:
            started = time.monotonic()
            result = provider.run(lambda event: None, lambda: time.monotonic() - started > 480)
            write(directory / "result.json", result)
            proof.update(runtime=result["runtime"], results=result["probe_results"])
            proof["passed"] = all(item["passed"] for item in proof["results"])
    except Exception as error:
        proof["error_type"] = type(error).__name__
    finally:
        if provider.stream:
            provider.stream.stop()
            provider.stream = None
        proof["remote_stopped"] = provider.stop()
        proof["private_staging_cleaned"] = provider.cleanup() if proof["remote_stopped"] else False
        write(directory / "proof.json", proof)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "stop"])
    parser.add_argument("--username", default="truongphucdev")
    parser.add_argument("--directory")
    args = parser.parse_args()
    result = prepare(args.username) if args.action == "prepare" else run(args.directory, stop=args.action == "stop")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
