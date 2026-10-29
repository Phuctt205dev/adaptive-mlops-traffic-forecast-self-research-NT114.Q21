"""Isolated GPU SHAP check using tiny untrained LSTM/GRU, never app data.

prepare is offline; run explicitly creates one private Kaggle notebook. Its
resources are deleted only after the provider confirms that execution stopped.
"""
import argparse
import hashlib
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
from src.cloud_training.kaggle_execution import KaggleExecution, TrainingCancelled, initial_ledger
from src.cloud_training.kaggle_transport import SDKTransport

CHILD = r'''
import hashlib,importlib.metadata,json,os,sys,traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
job=json.loads(Path(sys.argv[sys.argv.index("--job")+1]).read_text())
output=Path(sys.argv[sys.argv.index("--output")+1]);output.mkdir(parents=True,exist_ok=False)
from scripts.cloud_training.original_execute_branch import prepare_runtime
prepare_runtime(job)
import joblib,numpy as np,tensorflow as tf
import shap
from tensorflow import keras
from sklearn.preprocessing import StandardScaler
from src.models.recurrent import build_recurrent_model
from src.shap_reporting import explain_saved_model
from src.cloud_training.artifacts import write_bundle
devices=tf.config.list_physical_devices("GPU")
if not devices:raise RuntimeError("GPU check requires an actual GPU")
for device in devices:tf.config.experimental.set_memory_growth(device,True)
candidates=[]
diagnostics=[]
original_gradient=shap.GradientExplainer
class ObservedGradient:
    def __init__(self,*args,**kwargs):self.explainer=original_gradient(*args,**kwargs)
    def shap_values(self,*args,**kwargs):
        try:return self.explainer.shap_values(*args,**kwargs)
        except Exception as error:
            frames=traceback.extract_tb(error.__traceback__)
            diagnostics.append({"error_type":type(error).__name__,"location":Path(frames[-1].filename).name+":"+str(frames[-1].lineno),
                                "message":str(error)[:800]})
            raise
shap.GradientExplainer=ObservedGradient
for name in ("LSTM","GRU"):
    diagnostics.clear()
    keras.backend.clear_session();keras.utils.set_random_seed(42)
    inputs=np.random.default_rng(42).normal(size=(4,3,2)).astype("float32")
    scaler=StandardScaler().fit(np.array([[100.],[200.]]))
    preprocessor=output/(name+"_preprocessor.pkl")
    joblib.dump({"sequence_feature_names":["a","b"],"target_scaler":scaler},preprocessor)
    with tf.device("/GPU:0"):
        model=build_recurrent_model(name,(3,2),recurrent_units=2,dense_units=2)
        expected=model(inputs,training=False).numpy()
        model_path=output/(name+".keras");model.save(model_path)
        before=sha=hashlib.sha256(model_path.read_bytes()).hexdigest()
        report=explain_saved_model(model,inputs,model_name=name,model_path=model_path,
            output_path=output/(name+"_shap.json"),sequence=True,feature_names=["a","b"],target_scale=50.,
            context={"region_id":job["context"]["region_id"],"dataset_id":job["context"]["dataset_id"],
                     "training_run_id":job["context"]["run_id"],"preprocessor_sha256":hashlib.sha256(preprocessor.read_bytes()).hexdigest()},
            config={"max_samples":2,"background_samples":2,"batch_size":1,"nsamples":16})
    unchanged=before==hashlib.sha256(model_path.read_bytes()).hexdigest() and np.allclose(model(inputs,training=False).numpy(),expected)
    compatibility=output/(name+".compatibility.npz")
    np.savez_compressed(compatibility,X=inputs,expected=expected,raw_expected=scaler.inverse_transform(expected).reshape(-1))
    candidates.append({"best_model_name":name,"model_file":str(model_path),"versioned_model_file":str(model_path),
        "preprocessor_file":str(preprocessor),"compatibility_file":str(compatibility),"model_family":"neural_sequence",
        "shap_report":report,"model_unchanged":bool(unchanged),"diagnostics":list(diagnostics)})
    keras.backend.clear_session()
write_bundle({"branch":job["context"]["branch"],"status":"completed","candidates":candidates,
    "runtime":{"tensorflow":tf.__version__,"keras":keras.__version__,"shap":importlib.metadata.version("shap"),
               "gpu_count":len(devices)},"fit_called":False},job["context"],output)
'''


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def prepare(username):
    directory = ROOT / "results/shap-neural/kaggle" / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    dataset = directory / "synthetic.csv"
    dataset.write_bytes(b"synthetic_only\n1\n")
    configuration = {"selected_models": ["lstm", "gru"], "training_backend": "kaggle", "shap": {"enabled": True}}
    context = {key: str(uuid.uuid4()) for key in ("run_id", "region_id", "dataset_id", "attempt_id")}
    context.update(branch="lstm", configuration_sha256=configuration_digest(configuration))
    snapshot = prepare_package(ROOT, dataset, configuration, context, directory / "input", sha256_file(dataset), {"shap": "0.50.0"})
    archive = directory / "input/source.bin"
    with zipfile.ZipFile(archive) as source:
        entries = {item.filename: source.read(item) for item in source.infolist()}
    entries["scripts/cloud_training/original_execute_branch.py"] = entries["scripts/cloud_training/execute_branch.py"]
    entries["scripts/cloud_training/execute_branch.py"] = CHILD.encode()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as destination:
        for name, source in entries.items(): destination.writestr(name, source)
    snapshot["files"]["source.bin"] = {"sha256": sha256_file(archive), "size": archive.stat().st_size}
    write(directory / "ledger.json", initial_ledger(snapshot, username))
    write(directory / "check.json", {"username": username, "synthetic_only": True, "fit_called": False,
        "source_sha256": {name: sha256_file(ROOT / name) for name in (
            "src/shap_reporting.py", "src/models/recurrent.py", "scripts/cloud_training/execute_branch.py")}})
    return directory


def run(directory, *, stop=False):
    directory = Path(directory).resolve(strict=True)
    directory.relative_to((ROOT / "results/shap-neural/kaggle").resolve())
    check = json.loads((directory / "check.json").read_text())
    provider = KaggleExecution(SDKTransport(check["username"], repository=ROOT,
        python=ROOT / "results/kaggle-poc/venv/Scripts/python.exe"), directory,
        json.loads((directory / "ledger.json").read_text()), lambda value: write(directory / "ledger.json", value))
    proof = {"synthetic_only": True, "fit_called": False, "source_sha256": check["source_sha256"], "passed": False}
    try:
        if not stop:
            started = time.monotonic()
            result = provider.run(lambda event: None, lambda: time.monotonic() - started > 480)
            write(directory / "result.json", result)
            proof.update(runtime=result["runtime"], results=[{
                "model": item["best_model_name"], "status": item["shap_report"]["status"],
                "model_unchanged": item["model_unchanged"], "diagnostics": item.get("diagnostics", [])} for item in result["candidates"]])
            proof["passed"] = all(item["status"] == "completed" and item["model_unchanged"] for item in proof["results"])
    except Exception as error:
        proof["error_type"] = type(error).__name__
    finally:
        if provider.stream:
            provider.stream.stop(); provider.stream = None
        proof["remote_stopped"] = provider.stop()
        proof["private_staging_cleaned"] = provider.cleanup() if proof["remote_stopped"] else False
        proof["passed"] = proof["passed"] and proof["private_staging_cleaned"]
        write(directory / "proof.json", proof)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "stop"])
    parser.add_argument("--username", default="truongphucdev")
    parser.add_argument("--directory")
    args = parser.parse_args()
    if args.action == "prepare":
        print(json.dumps({"prepared": str(prepare(args.username)), "submitted": False}))
        return 0
    proof = run(args.directory, stop=args.action == "stop")
    print(json.dumps(proof))
    return 0 if proof["passed"] or args.action == "stop" and proof["private_staging_cleaned"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
