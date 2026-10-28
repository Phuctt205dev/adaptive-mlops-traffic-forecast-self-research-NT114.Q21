"""Isolated RF Kaggle-to-worker proof. Synthetic only; no business DB/S3 writes."""
import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[2]
REMOTE = r'''import csv,hashlib,importlib.metadata,json,math,os,subprocess,sys,time,traceback
from datetime import datetime,timedelta
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
request=json.load(sys.stdin)
project=Path('/opt/traffic-cloud/project');sys.path.insert(0,str(project))
from src.cloud_training.package import configuration_digest,prepare_package
from src.cloud_training.kaggle_execution import KaggleExecution,initial_ledger
from src.cloud_training.kaggle_transport import SDKTransport
if request.get('candidate_source'):
 import base64,types
 candidate=types.ModuleType('transfer_candidate')
 exec(compile(base64.b64decode(request['candidate_source']),'transfer_candidate.py','exec'),candidate.__dict__)
 KaggleExecution,initial_ledger=candidate.KaggleExecution,candidate.initial_ledger
import requests
root=Path('/tmp')/('traffic-transfer-proof-'+request['attempt_id']);root.mkdir(exist_ok=False)
def save(value):
 (root/'ledger.tmp').write_text(json.dumps(value));(root/'ledger.tmp').replace(root/'ledger.json')
out={'synthetic_only':True,'business_db_s3_written':False,'reference':'truongphucdev/traffic-train-'+request['attempt_id'].replace('-',''),'downloads':[]}
provider=None
original_send=requests.Session.send
def observed_send(session,prepared,**kwargs):
 entry={'host':urlparse(prepared.url).hostname};out['downloads'].append(entry)
 try:
  response=original_send(session,prepared,**kwargs);entry['http_status']=response.status_code
  original_content=response.iter_content
  def content(*args,**options):
   try:yield from original_content(*args,**options)
   except BaseException as error:entry['body_error_type']=type(error).__name__;raise
  response.iter_content=content;return response
 except BaseException as error:entry['error_type']=type(error).__name__;raise
requests.Session.send=observed_send
try:
 with urlopen('http://127.0.0.1:8000/health/integration',timeout=3) as response:before=json.load(response)
 if before.get('busy'):raise RuntimeError('Worker busy')
 dataset=root/'synthetic.csv'
 columns=['date_time','is_holiday','weather_type','weather_description','air_pollution_index','temperature','humidity','wind_speed','wind_direction','visibility_in_miles','dew_point','rain_p_h','snow_p_h','clouds_all','traffic_volume']
 with dataset.open('w',newline='') as stream:
  writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader()
  for hour in range(request.get('rows',1500)):writer.writerow({'date_time':(datetime(2026,1,1)+timedelta(hours=hour)).isoformat(),'is_holiday':'No Holiday','weather_type':'Clear','weather_description':'synthetic','air_pollution_index':30,'temperature':290,'humidity':60,'wind_speed':2,'wind_direction':180,'visibility_in_miles':10,'dew_point':15,'rain_p_h':0,'snow_p_h':0,'clouds_all':20,'traffic_volume':round(1000+200*math.sin(hour/12)+hour%40,3)})
 config={'training_backend':'kaggle','_execution_protocol':2,'selected_models':['random_forest_lag'],'train_start_date':'2026-01-01','train_end_date':'2026-02-15','cv_splits':3,'random_state':42,'shap':{'enabled':False}}
 if request.get('shap'):
  config['shap']={'enabled':True,'max_samples':4,'background_samples':4,'max_seconds':30}
 if request.get('rows',1500)>1500:config['train_end_date']='2026-06-15'
 context={**{key:request[key] for key in ['run_id','region_id','dataset_id','attempt_id']},'branch':'tree','configuration_sha256':configuration_digest(config)}
 profile={name:importlib.metadata.version(name) for name in ['scikit-learn','joblib','pandas']}
 if request.get('shap'):
  sys.path.insert(0,'/opt/traffic-shap-deps')
  profile['shap']=importlib.metadata.version('shap')
 snapshot=prepare_package(project,dataset,config,context,root/'input',hashlib.sha256(dataset.read_bytes()).hexdigest(),profile)
 ledger=initial_ledger(snapshot,'truongphucdev');save(ledger)
 transport=SDKTransport('truongphucdev',python=os.environ['KAGGLE_PYTHON'],repository=project)
 provider=KaggleExecution(transport,root,ledger,save)
 result=provider.run(lambda event:None,lambda:False)
 out['output_received']=result.get('status')=='completed'
 entries=[{key:candidate.get(key) for key in ['model_file','model_family','preprocessor_file','compatibility_file']} for candidate in result['candidates']]
 (root/'serving.json').write_text(json.dumps(entries))
 check=subprocess.run([sys.executable,'-B',str(project/'scripts/cloud_training/verify_execution.py'),'--request',str(root/'serving.json')],capture_output=True,timeout=180)
 out['cpu_reload_verified']=check.returncode==0
except BaseException as error:
 frames=traceback.extract_tb(error.__traceback__)
 out.update(error_type=type(error).__name__,location=Path(frames[-1].filename).name+':'+str(frames[-1].lineno) if frames else None,operation=getattr(error,'operation',None),category=getattr(error,'category',None))
finally:
 out['external_write_started']=provider is not None
 if provider:
  try:out['remote_stopped']=provider.stop();out['private_staging_cleaned']=provider.cleanup() if out['remote_stopped'] else False
  except BaseException as error:out['cleanup_error_type']=type(error).__name__
 try:
  with urlopen('http://127.0.0.1:8000/health/integration',timeout=3) as response:after=json.load(response)
  out['worker_operation_count_unchanged']=not after.get('busy') and before['operation_count']==after['operation_count']
 except BaseException:out['worker_operation_count_unchanged']=False
 out['proof_passed']=all(out.get(key) is True for key in ['output_received','cpu_reload_verified','private_staging_cleaned','worker_operation_count_unchanged'])
 (root/'proof.json').write_text(json.dumps(out))
 print('TRAFFIC_TRANSFER_PROOF '+json.dumps(out),flush=True)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--proof", required=True)
    parser.add_argument("--candidate", action="store_true")
    parser.add_argument("--shap", action="store_true")
    parser.add_argument("--rows", type=int, default=1500)
    args = parser.parse_args()
    from manage_rollout import Rollout
    proof = Path(args.proof)
    if proof.exists():
        raise RuntimeError("Keep an existing proof; do not duplicate its fit.")
    request = {key: str(uuid.uuid4()) for key in ("run_id", "region_id", "dataset_id", "attempt_id")}
    if not 1500 <= args.rows <= 6000:
        raise ValueError("The isolated synthetic check is bounded to 1500..6000 rows.")
    request.update(rows=args.rows, shap=args.shap)
    if args.candidate:
        request["candidate_source"] = base64.b64encode((ROOT / "src/cloud_training/kaggle_execution.py").read_bytes()).decode()
    context = {key: value for key, value in request.items() if key != "candidate_source"}
    journal = {"context": context, "candidate_source_sha256": __import__('hashlib').sha256((ROOT / "src/cloud_training/kaggle_execution.py").read_bytes()).hexdigest() if args.candidate else None,
               "write_pending": True, "synthetic_only": True, "business_db_s3_written": False,
               "worker_directory": "/tmp/traffic-transfer-proof-" + request["attempt_id"], "checked_at": datetime.now(timezone.utc).isoformat()}
    proof.write_text(json.dumps(journal, indent=2), encoding="utf-8")
    rollout = Rollout(args.kubectl, ROOT / "results/cloud-training/review-final", ROOT / "results/cloud-training/snapshot-20261028-v2.json")
    command = [args.kubectl, "--request-timeout=600s", "-n", "traffic-mlops", "exec", "-i", rollout.pod("traffic-training-tree"),
               "-c", "training-worker", "--", "python", "-B", "-c", REMOTE]
    result = subprocess.run(command, input=json.dumps(request), text=True, encoding="utf-8", capture_output=True, timeout=620)
    lines = [line.removeprefix("TRAFFIC_TRANSFER_PROOF ") for line in result.stdout.splitlines() if line.startswith("TRAFFIC_TRANSFER_PROOF ")]
    if len(lines) != 1:
        print(json.dumps({"proof_unconfirmed": True, "worker_directory": journal["worker_directory"]}))
        return 2
    outcome = json.loads(lines[0]);journal.update(result=outcome, write_pending=bool(outcome.get("external_write_started", True)) and not outcome.get("private_staging_cleaned", False))
    proof.write_text(json.dumps(journal, indent=2), encoding="utf-8")
    print(json.dumps(journal))
    return 0 if outcome.get("proof_passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
