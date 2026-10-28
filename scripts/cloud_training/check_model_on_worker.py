"""Reload only this synthetic check's verified model/probe in a temporary worker process."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.cloud_training.artifacts import read_bundle
from src.cloud_training.probe import write_json

REMOTE = '''import base64,hashlib,json,os,sys,tempfile
from pathlib import Path
from urllib.request import urlopen
os.environ["CUDA_VISIBLE_DEVICES"]="-1"
os.environ["TF_CPP_MIN_LOG_LEVEL"]="3"
try:
 request=json.load(sys.stdin)
 with urlopen("http://127.0.0.1:8000/health/integration",timeout=3) as r:before=json.load(r)
 if before.get("busy") or before.get("pod")!=request["pod"]:raise RuntimeError("Worker is not idle")
 with tempfile.TemporaryDirectory(prefix="traffic-cloud-model-check-") as directory:
  root=Path(directory)
  for name,item in request["files"].items():
   if Path(name).name!=name:raise ValueError("Unsafe check path")
   data=base64.b64decode(item["data"],validate=True)
   if len(data)!=item["size"] or hashlib.sha256(data).hexdigest()!=item["sha256"]:raise ValueError("Check file hash differs")
   (root/name).write_bytes(data)
  item={key:str(root/name) for key,name in request["candidate"].items() if key!="model_family"}
  item["model_family"]=request["candidate"]["model_family"]
  scope={"Path":Path}
  exec(request["verification_source"],scope)
  scope["verify"]([item])
 with urlopen("http://127.0.0.1:8000/health/integration",timeout=3) as r:after=json.load(r)
 if after.get("busy") or after.get("operation_count")!=before.get("operation_count"):raise RuntimeError("Worker changed during the check")
 print("TRAFFIC_MODEL_CHECK "+json.dumps({"serving_cpu_reload_verified":True,"fit_called":False,"business_db_s3_written":False,"operation_count":after["operation_count"]}))
except Exception as error:
 print("TRAFFIC_MODEL_CHECK "+json.dumps({"serving_cpu_reload_verified":False,"error_type":type(error).__name__}))
 raise SystemExit(2)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--pod", required=True)
    args = parser.parse_args()
    try:
        directory = Path(args.directory).resolve(strict=True)
        directory.relative_to((ROOT / "results/kaggle-poc/integration").resolve(strict=True))
        check = json.loads((directory / "check.json").read_text())
        if check.get("synthetic_only") is not True or check.get("purpose") not in {"tree-model", "lstm-model", "gru-model", "tree-suite"}:
            raise ValueError("This verifier only accepts owned synthetic model checks.")
        ledger = json.loads((directory / "ledger.json").read_text())
        result = read_bundle(directory / "output", ledger["snapshot"]["context"])
        candidate = result["candidates"][0]
        files, mapping, total = {}, {"model_family": candidate["model_family"]}, 0
        for key in ("model_file", "compatibility_file", "preprocessor_file"):
            if not candidate.get(key):
                continue
            path = Path(candidate[key])
            data = path.read_bytes(); total += len(data)
            if total > 64 * 1024 * 1024:
                raise ValueError("Synthetic verifier transfer exceeds budget.")
            name = key + path.suffix
            mapping[key] = name
            files[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "data": base64.b64encode(data).decode()}
        import inspect
        from scripts.cloud_training.verify_execution import verify
        request = {"pod": args.pod, "files": files, "candidate": mapping, "verification_source": inspect.getsource(verify)}
        command = [args.kubectl, "--request-timeout=20s", "-n", "traffic-mlops", "exec", "-i", args.pod,
                   "-c", "training-worker", "--", "python", "-B", "-c", REMOTE]
        response = subprocess.run(command, input=json.dumps(request), text=True, encoding="utf-8",
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
        lines = [line for line in response.stdout.splitlines() if line.startswith("TRAFFIC_MODEL_CHECK ")]
        if len(lines) != 1:
            raise RuntimeError("The worker did not return a single model proof.")
        proof = json.loads(lines[0].removeprefix("TRAFFIC_MODEL_CHECK "))
        write_json(directory / "serving-proof.json", proof)
        print(json.dumps(proof))
        return 0 if proof.get("serving_cpu_reload_verified") else 2
    except Exception as error:
        print(json.dumps({"model_check_failed": True, "error_type": type(error).__name__, "raw_details_omitted": True}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
