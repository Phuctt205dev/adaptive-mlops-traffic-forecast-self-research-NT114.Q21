"""Inject one lost status read, then verify the retry through the real worker SDK.

Reads an existing completed synthetic notebook. No upload, fit, stop or cleanup.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
REMOTE = r'''import base64,json,os,subprocess,sys
request=json.load(sys.stdin)
child=r"""import base64,json,os,subprocess,sys,types
request=json.load(sys.stdin)
module=types.ModuleType('monitor_candidate');exec(compile(base64.b64decode(request['source']),'monitor_candidate.py','exec'),module.__dict__)
original=subprocess.run;operations=[]
def injected(*args,**kwargs):
 payload=json.loads(kwargs.get('input','{}'));operations.append(payload.get('operation'))
 if payload.get('operation')=='status' and len(operations)==1:
  return subprocess.CompletedProcess(args[0],2,json.dumps({'ok':False,'error_type':'ConnectTimeout','http_status':None}))
 return original(*args,**kwargs)
subprocess.run=injected
try:
 transport=module.SDKTransport('truongphucdev',python=os.environ['KAGGLE_PYTHON'],repository='/opt/traffic-cloud/project')
 result=transport.call('status',reference='truongphucdev/traffic-poc-fa7ec6cb09c644c6/1',timeout=75)
 print(json.dumps({'retry_recovered':result.get('state')=='COMPLETE','operations':operations,'injected_connect_timeout':True,'training_started':False,'business_db_s3_written':False}))
except BaseException as error:print(json.dumps({'retry_recovered':False,'operations':operations,'error_type':type(error).__name__}))
"""
try:
 result=subprocess.run([sys.executable,'-B','-c',child],input=json.dumps(request),text=True,cwd='/opt/traffic-cloud/project',capture_output=True,timeout=85)
 print(json.dumps(json.loads(result.stdout)))
except BaseException as error:print(json.dumps({'retry_recovered':False,'error_type':type(error).__name__}))
'''


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--proof", required=True)
    args = parser.parse_args()
    proof_path = Path(args.proof)
    if proof_path.exists():
        raise RuntimeError("Keep the existing proof unchanged.")
    from manage_rollout import Rollout
    rollout = Rollout(args.kubectl, ROOT / "results/cloud-training/cloud-result-fix", ROOT / "results/cloud-training/cloud-result-fix/baseline.json")
    source = (ROOT / "src/cloud_training/kaggle_transport.py").read_bytes()
    command = [args.kubectl, "--request-timeout=95s", "-n", "traffic-mlops", "exec", "-i", rollout.pod("traffic-training-worker"),
               "-c", "training-worker", "--", "python", "-B", "-c", REMOTE]
    result = subprocess.run(command, input=json.dumps({"source": base64.b64encode(source).decode()}), text=True, encoding="utf-8", capture_output=True, timeout=100)
    try:
        proof = json.loads(result.stdout)
        proof.update(transport_sha256=hashlib.sha256(source).hexdigest(), read_only=True)
        proof_path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
        print(json.dumps(proof))
        raise SystemExit(0 if proof.get("retry_recovered") and set(proof["operations"]) == {"status"} else 2)
    except ValueError:
        print(json.dumps({"retry_recovered": False, "raw_details_omitted": True}))
        raise SystemExit(2)
