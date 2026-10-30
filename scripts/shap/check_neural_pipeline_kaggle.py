"""Tiny synthetic GPU check through both CV folds, final fit, report and reload."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.shap import check_neural_fold_kaggle as probe

CHILD = r'''
import json,sys,traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
job=json.loads(Path(sys.argv[sys.argv.index("--job")+1]).read_text())
output=Path(sys.argv[sys.argv.index("--output")+1]);output.mkdir(parents=True,exist_ok=False)
from scripts.cloud_training.original_execute_branch import prepare_runtime
prepare_runtime(job)
import joblib,numpy as np,pandas as pd,tensorflow as tf
from tensorflow import keras
from src import time_series_cross_validation as cv
from src.training_progress import reporting
from src.cloud_training.artifacts import write_bundle
from src.time_series_preprocess import ORIGINAL_COLUMN_ORDER,prepare_hourly_time_series
from src.time_series_features import create_time_series_features
if not tf.config.list_physical_devices("GPU"):raise RuntimeError("GPU required")
rng=np.random.default_rng(42)
raw=pd.DataFrame({column:np.arange(256,dtype=float) for column in ORIGINAL_COLUMN_ORDER})
raw["date_time"]=pd.date_range("2026-01-01",periods=256,freq="h")
raw["is_holiday"]=None;raw["weather_type"]="Clear";raw["weather_description"]="clear sky"
for column in ["temperature","humidity","rain_p_h"]:raw[column]=rng.normal(size=256)
raw["traffic_volume"]=10000+1000*(2*(np.arange(256)%2)-1)
hourly,audit,_=prepare_hourly_time_series(raw)
start,end=hourly.date_time.iloc[200],hourly.date_time.iloc[240]
# The fixture has 40 target hours, plus only causal context and reserved future.
features,_=create_time_series_features(hourly,audit)
original_datasets=cv._build_neural_fold_datasets
def bounded_inputs(*args,selected_groups=None,**kwargs):
 return original_datasets(*args,selected_groups=selected_groups or ["temperature","humidity","rain_p_h"],**kwargs)
cv._build_neural_fold_datasets=bounded_inputs
results=[]
for name in ("LSTM","GRU"):
 result={"model":name,"passed":False};events=[]
 try:
  directory=output/name;directory.mkdir()
  with reporting(events.append):
   report=cv._run_neural_cross_validation(name,features,hourly,audit,2,6,1,8,42,
     str(directory/"model.keras"),str(directory/"pre.joblib"),str(directory/"pred.csv"),0,
     train_start_date=start.isoformat(),train_end_date=end.isoformat(),final_test_ratio=.15,
     shap_config={"max_samples":2,"background_samples":2,"batch_size":1,"nsamples":8,"max_seconds":60},
     feature_selection={"enabled":True,"retain_share":.5,"max_mae_increase":.1})
  assert set(report["cv_metrics"])=={"MAE","RMSE","MAPE","WAPE","R2"}
  assert all(set(fold["validation_metrics"])==set(report["cv_metrics"]) for fold in report["folds"])
  assert report["shap_report"]["status"]=="completed"
  selection=report["feature_selection_result"]
  assert selection["mode"]=="reduced"
  model=keras.models.load_model(str(directory/"model.keras"),compile=False,safe_mode=True)
  pre=joblib.load(directory/"pre.joblib")
  with np.load(report["compatibility_file"],allow_pickle=False) as data:
   assert data["X"].shape[-1]==len(pre["sequence_feature_names"])
   np.testing.assert_allclose(model(data["X"],training=False).numpy(),data["expected"],rtol=1e-4,atol=1e-5)
  result.update(passed=True,full_pipeline_completed=True,cv_metric_names=list(report["cv_metrics"]),
    selection_mode=selection["mode"],history_context_preserved=pd.Timestamp(report["history_context"]["start"])<start,
    training_targets_in_window=all(pd.Timestamp(fold["train"]["start"])>=start for fold in report["folds"]),
    final_report_ready=True,reload_predict=True,cv_folds=len(report["folds"]),max_epochs_per_fit=1)
 except Exception as error:
  frames=traceback.extract_tb(error.__traceback__)
  project=[f for f in frames if "/src/" in f.filename.replace("\\","/")]
  frame=project[-1] if project else frames[-1]
  result.update(error_type=type(error).__name__,source=Path(frame.filename).name,line=frame.lineno,
    final_source=Path(frames[-1].filename).name,final_line=frames[-1].lineno)
 results.append(result);keras.backend.clear_session()
write_bundle({"branch":"lstm","status":"completed","candidates":[],"probe_results":results,
 "fit_called":True,"synthetic_only":True,"runtime":{"tensorflow":tf.__version__,"keras":keras.__version__}},job["context"],output)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "stop"])
    parser.add_argument("--username", default="truongphucdev")
    parser.add_argument("--directory")
    args = parser.parse_args()
    if args.action == "prepare":
        result = probe.prepare(args.username, child_source=CHILD)
        result["full_pipeline_probe"] = True
    else:
        result = probe.run(args.directory, stop=args.action == "stop")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
