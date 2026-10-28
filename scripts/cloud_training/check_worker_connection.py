"""Check the proposed RPC in a temporary worker process; never fit a model.

read is the default. upload creates one tiny private synthetic dataset and
cleans it. Every write has a local journal; cleanup reconciles that exact ref.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[2]
REMOTE = r'''import base64,contextlib,json,os,subprocess,sys
request=json.load(sys.stdin)
child=r"""import base64,contextlib,json,sys,tempfile,traceback,types
from pathlib import Path
request=json.load(sys.stdin);stdout=sys.stdout
class Quiet:
 def write(self,s):return len(s)
 def flush(self):pass
out={'phase':'authenticate','synthetic_only':True,'training_started':False,'reference':request.get('reference')}
api=None
try:
 module=types.ModuleType('connection_rpc_probe');exec(compile(base64.b64decode(request['rpc_source']),'connection_rpc_probe.py','exec'),module.__dict__)
 with contextlib.redirect_stdout(Quiet()),contextlib.redirect_stderr(Quiet()):
  api=module.api_for('truongphucdev')
  if request['mode']=='read':
   out['phase']='quota_read';out['doctor']=module.operation(api,'doctor',{'username':'truongphucdev'},stdout);out['probe_passed']=True
  elif request['mode']=='upload':
   with tempfile.TemporaryDirectory(prefix='traffic-connection-probe-') as directory:
    root=Path(directory);(root/'synthetic.csv').write_text('date_time,traffic_volume\n2026-01-01,1\n')
    (root/'dataset-metadata.json').write_text(json.dumps({'id':request['reference'],'title':request['reference'].split('/')[1],'licenses':[{'name':'other'}],'description':'Temporary synthetic upload probe. All rights reserved.'}))
    out['phase']='dataset_create';module.operation(api,'dataset_create',{'directory':directory},stdout)
    out['phase']='dataset_check'
    for retry in range(6):
     try:info=module.operation(api,'dataset_check',{'reference':request['reference']},stdout);break
     except Exception as error:
      if getattr(getattr(error,'response',None),'status_code',None) not in {403,404} or retry==5:raise
      import time;time.sleep(3)
    out['private_version_verified']=info['private'] and info['version']==1
    out['probe_passed']=out['private_version_verified']
  else:out['probe_passed']=True
except BaseException as error:
 frames=traceback.extract_tb(error.__traceback__)
 out.update(probe_passed=False,error_type=type(error).__name__,http_status=getattr(getattr(error,'response',None),'status_code',None),location=Path(frames[-1].filename).name+':'+str(frames[-1].lineno) if frames else None)
finally:
 if api is not None and request['mode'] in {'upload','cleanup'}:
  try:
   with contextlib.redirect_stdout(Quiet()),contextlib.redirect_stderr(Quiet()):
    query={'kind':'dataset','reference':request['reference']}
    if module.operation(api,'resource_exists',query,stdout)['exists']:
     module.operation(api,'dataset_check',{'reference':request['reference']},stdout)
     module.operation(api,'cleanup',{'dataset':request['reference']},stdout)
    out['cleaned']=not module.operation(api,'resource_exists',query,stdout)['exists']
  except BaseException as error:out.update(cleaned=False,cleanup_error_type=type(error).__name__)
stdout.write(json.dumps(out)+'\n')
"""
try:
 result=subprocess.run([os.environ['KAGGLE_PYTHON'],'-B','-c',child],input=json.dumps(request),text=True,cwd='/opt/traffic-cloud/project',capture_output=True,timeout=85)
 print(json.dumps(json.loads(result.stdout)))
except BaseException as error:print(json.dumps({'probe_passed':False,'result_unconfirmed':True,'error_type':type(error).__name__}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--mode", choices=["read", "upload", "cleanup"], default="read")
    parser.add_argument("--deployment", choices=["traffic-training-tree", "traffic-training-worker"], default="traffic-training-tree")
    parser.add_argument("--proof", required=True)
    args = parser.parse_args()
    try:
        from manage_rollout import Rollout
        root = ROOT / "results/cloud-training"
        proof = Path(args.proof).resolve()
        if not proof.is_relative_to(root.resolve()):
            raise ValueError("Proof must stay in results/cloud-training.")
        if args.mode == "cleanup":
            journal = json.loads(proof.read_text(encoding="utf-8"))
            reference = journal["reference"]
            if not reference.startswith("truongphucdev/traffic-probe-") or len(reference.split("traffic-probe-")[1]) != 32:
                raise ValueError("This journal does not describe an isolated connection probe.")
            int(reference.split("traffic-probe-")[1], 16)
        else:
            if proof.exists():
                raise ValueError("Keep existing proof unchanged; use cleanup to reconcile an uncertain write.")
            reference = "truongphucdev/traffic-probe-" + uuid.uuid4().hex if args.mode == "upload" else None
            journal = {"reference": reference, "mode": args.mode, "synthetic_only": True, "training_started": False,
                       "write_pending": args.mode == "upload", "deployment": args.deployment}
        source = (ROOT / "src/cloud_training/kaggle_rpc.py").read_bytes()
        journal.update(rpc_sha256=hashlib.sha256(source).hexdigest(), checked_at=datetime.now(timezone.utc).isoformat())
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text(json.dumps(journal, indent=2), encoding="utf-8")
        rollout = Rollout(args.kubectl, ROOT / "results/cloud-training/review-final", ROOT / "results/cloud-training/snapshot-20261028-v2.json")
        pod = rollout.pod(args.deployment)
        command = [args.kubectl, "--request-timeout=95s", "-n", "traffic-mlops", "exec", "-i", pod,
                   "-c", "training-worker", "--", "python", "-B", "-c", REMOTE]
        result = subprocess.run(command, input=json.dumps({"mode": args.mode, "reference": reference,
                                "rpc_source": base64.b64encode(source).decode()}), text=True, encoding="utf-8", capture_output=True, timeout=100)
        response = json.loads(result.stdout)
        journal.update(last_action=args.mode, pod=pod)
        journal["cleanup_result" if args.mode == "cleanup" else "result"] = response
        if response.get("cleaned") is True:
            journal["write_pending"] = False
        proof.write_text(json.dumps(journal, indent=2), encoding="utf-8")
        print(json.dumps(journal))
        return 0 if response.get("probe_passed") and (args.mode == "read" or response.get("cleaned")) else 2
    except Exception as error:
        print(json.dumps({"probe_unconfirmed": True, "error_type": type(error).__name__, "raw_details_omitted": True}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
