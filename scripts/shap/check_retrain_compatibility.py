"""Read-only worker check: tiny untrained models, new masks, SHAP; no fit/lifespan."""
import argparse
import base64
import json
from pathlib import Path
import subprocess

REMOTE = r'''
import base64,importlib.util,json,os,sys,tempfile
from pathlib import Path
request=json.load(sys.stdin)
os.environ.update(CUDA_VISIBLE_DEVICES="-1",TF_CPP_MIN_LOG_LEVEL="3",TF_NUM_INTRAOP_THREADS="1",TF_NUM_INTEROP_THREADS="1")
try:
 with tempfile.TemporaryDirectory(prefix="selection-check-") as temporary:
  root=Path(temporary)/"cloud";data=root/"..check";data.mkdir(parents=True)
  for name,value in request["files"].items():
   path=data/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(base64.b64decode(value,validate=True))
  (root/"..data").symlink_to(data.name,target_is_directory=True)
  for path in data.iterdir():(root/path.name).symlink_to(Path("..data")/path.name,target_is_directory=path.is_dir())
  os.environ.update(TRAFFIC_CLOUD_MODE="worker",TRAINING_CONTROL_ENABLED="false",CLOUD_TRAINING_ENABLED="false")
  spec=importlib.util.spec_from_file_location("selection_runtime_check",root/"cloud_training_runtime.py")
  runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime);app=runtime.build_app(root=root)
  import numpy as np,pandas as pd,tensorflow as tf
  from src.feature_selection import rank_training_features,wrap_tree_model
  from src.lstm_sequences import SEQUENCE_NUMERIC_COLUMNS,fit_sequence_preprocessors,transform_sequence_source
  from src.models.recurrent import build_recurrent_model
  from sklearn.ensemble import RandomForestRegressor
  import joblib
  source=pd.DataFrame({name:np.arange(16,dtype=float) for name in SEQUENCE_NUMERIC_COLUMNS})
  source["traffic_volume"]=np.arange(16,dtype=float);source["weather_type"]="Clear"
  source["date_time"]=pd.date_range("2026-01-01",periods=16,freq="h")
  pre=fit_sequence_preprocessors(source,np.arange(4,12));full=transform_sequence_source(source,pre)
  names=pre["sequence_feature_names"];indices=[names.index(x) for x in ["temperature","hour_sin","hour_cos"]]
  pre["all_sequence_feature_names"]=names;pre["selected_feature_indices"]=indices
  pre["sequence_feature_names"]=[names[i] for i in indices]
  joblib.dump(pre,Path(temporary)/"pre.joblib");pre=joblib.load(Path(temporary)/"pre.joblib")
  selected=transform_sequence_source(source,pre);np.testing.assert_array_equal(selected,full[:,indices])
  X=np.stack([selected[i:i+3] for i in range(4)]).astype(np.float32);results=[]
  for name in ["LSTM","GRU"]:
   model=build_recurrent_model(name,input_shape=X.shape[1:]);expected=model(X,training=False).numpy()
   groups=rank_training_features(model,X,pre["sequence_feature_names"],sequence=True,
       shap_config={"max_samples":2,"background_samples":2,"batch_size":1,"nsamples":8,"max_seconds":60})
   path=Path(temporary)/(name+".keras");model.save(path)
   restored=tf.keras.models.load_model(path,compile=False,safe_mode=True)
   np.testing.assert_allclose(restored(X,training=False).numpy(),expected,rtol=1e-5,atol=1e-6)
   assert groups and restored.input_shape[-1]==len(pre["sequence_feature_names"])
   results.append({"model":name,"mask_roundtrip":True,"shap_ranking":True,"reload_predict":True})
   del model,restored;tf.keras.backend.clear_session()
  print("SELECTION_CHECK "+json.dumps({"passed":True,"bundle_id":request["bundle_id"],"fit_called":False,
     "lifespan_started":False,"tensorflow":tf.__version__,"results":results}))
except Exception as error:
 import traceback
 frames=traceback.extract_tb(error.__traceback__)
 print("SELECTION_CHECK "+json.dumps({"passed":False,"error_type":type(error).__name__,
    "location":Path(frames[-1].filename).name+":"+str(frames[-1].lineno) if frames else "unknown"}))
 raise SystemExit(2)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", default="C:/Users/ADMIN/AppData/Local/Programs/kubectl/kubectl.exe")
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    directory = Path(args.directory)
    review = json.loads((directory / "review.json").read_text())
    baseline = json.loads((directory / "baseline.json").read_text())
    resources = json.loads((directory / "configmaps.json").read_text())["items"]
    code = next(item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-")
                and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
    patch = json.loads((directory / "traffic-training-worker.patch.json").read_text())
    items = patch["spec"]["template"]["spec"]["volumes"][0]["configMap"]["items"]
    files = {item["path"]: base64.b64encode(code["data"][item["key"]].encode()).decode() for item in items}
    runtime = baseline["runtime"]["traffic-training-worker"]
    response = subprocess.run([args.kubectl, "--request-timeout=120s", "-n", "traffic-mlops", "exec", "-i",
        runtime["pod"], "-c", runtime["main_container"], "--", "python", "-B", "-c", REMOTE],
        input=json.dumps({"files": files, "bundle_id": review["bundle_id"]}), text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=150)
    lines = [line for line in response.stdout.splitlines() if line.startswith("SELECTION_CHECK ")]
    proof = json.loads(lines[0].removeprefix("SELECTION_CHECK ")) if len(lines) == 1 else {"passed": False, "reason": "missing_proof"}
    if response.returncode:
        proof["passed"] = False
    (directory / "selection-compatibility.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps(proof))
    return 0 if proof.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
