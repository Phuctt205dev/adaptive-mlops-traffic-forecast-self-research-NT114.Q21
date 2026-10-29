"""Explain tiny, untrained recurrent models in an isolated process.

No app startup, database, training fit, active model or business artifact reads.
The kubectl mode only executes this bounded check in an idle worker.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
REMOTE = r'''
import base64,hashlib,importlib.metadata,json,os,sys,tempfile,types
from pathlib import Path
request=json.load(sys.stdin)
os.environ.update(CUDA_VISIBLE_DEVICES="-1",TF_CPP_MIN_LOG_LEVEL="3",OMP_NUM_THREADS="1",
    TF_NUM_INTEROP_THREADS="1",TF_NUM_INTRAOP_THREADS="1")
before=None
if request.get("worker"):
    from urllib.request import urlopen
    before=json.load(urlopen("http://127.0.0.1:8000/health/integration",timeout=5))
    if before.get("busy"):raise RuntimeError("Worker is busy")
if Path("/opt/traffic-shap-deps").is_dir():sys.path.insert(0,"/opt/traffic-shap-deps")
import numpy as np
import tensorflow as tf
import shap
from tensorflow import keras
modules={}
for name,encoded in request["sources"].items():
    source=base64.b64decode(encoded,validate=True)
    module=types.ModuleType(name);sys.modules[name]=module
    exec(compile(source,name,"exec"),module.__dict__)
    modules[name]=module
results=[]
for name in ("LSTM","GRU"):
    keras.backend.clear_session()
    keras.utils.set_random_seed(42)
    model=modules["recurrent_check"].build_recurrent_model(name,(3,2),recurrent_units=2,dense_units=2)
    inputs=np.random.default_rng(42).normal(size=(4,3,2)).astype(np.float32)
    with tempfile.TemporaryDirectory(prefix="neural-shap-compatibility-") as directory:
        root=Path(directory);path=root/"model.keras";model.save(path)
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        result=modules["neural_shap_check"].explain_saved_model(model,inputs,model_name=name,
            model_path=path,output_path=root/"shap.json",sequence=True,feature_names=["a","b"],
            config={"max_samples":2,"background_samples":2,"batch_size":1,"nsamples":16})
        report=json.loads((root/"shap.json").read_text())
        results.append({"model":name,"status":result["status"],"error_type":report.get("error_type"),
            "sample_count":report.get("sample_count"),"explainer":report.get("explainer"),
            "model_unchanged":hashlib.sha256(path.read_bytes()).hexdigest()==digest})
    keras.backend.clear_session()
unchanged=True
if before is not None:
    after=json.load(urlopen("http://127.0.0.1:8000/health/integration",timeout=5))
    unchanged=before.get("operation_count")==after.get("operation_count") and not after.get("busy")
print("NEURAL_SHAP_PROOF "+json.dumps({"passed":all(x["status"]=="completed" and x["model_unchanged"] for x in results) and unchanged,
    "results":results,"tensorflow":tf.__version__,"keras":keras.__version__,"shap":shap.__version__,
    "fit_called":False,"business_data_used":False,"worker_operations_unchanged":unchanged,
    "source_sha256":{name:hashlib.sha256(base64.b64decode(value)).hexdigest() for name,value in request["sources"].items()}}),flush=True)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl")
    parser.add_argument("--pod")
    parser.add_argument("--container")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    sources = {"neural_shap_check": ROOT / "src/shap_reporting.py",
               "recurrent_check": ROOT / "src/models/recurrent.py"}
    request = {"sources": {name: base64.b64encode(path.read_bytes()).decode() for name, path in sources.items()},
               "worker": bool(args.kubectl)}
    if args.kubectl:
        if not args.pod or not args.container:
            parser.error("Remote checks require explicit --pod and --container")
        command = [args.kubectl, "--request-timeout=120s", "-n", "traffic-mlops", "exec", "-i",
                   args.pod, "-c", args.container, "--", "python", "-B", "-c", REMOTE]
    else:
        command = [sys.executable, "-B", "-c", REMOTE]
    result = subprocess.run(command, input=json.dumps(request), encoding="utf-8", capture_output=True, timeout=150)
    lines = [line.removeprefix("NEURAL_SHAP_PROOF ") for line in result.stdout.splitlines() if line.startswith("NEURAL_SHAP_PROOF ")]
    if result.returncode or len(lines) != 1:
        print(json.dumps({"passed": False, "checker_failed": True, "raw_details_omitted": True}))
        return 2
    proof = json.loads(lines[0])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps(proof))
    return 0 if proof["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
