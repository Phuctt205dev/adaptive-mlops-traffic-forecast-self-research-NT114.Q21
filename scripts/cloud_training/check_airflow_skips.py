"""Run mocked training tasks with REAL Airflow/XCom in isolated temporary SQLite."""
import argparse
import json
from pathlib import Path
import subprocess

REMOTE = r'''import json,os,subprocess,sys,tempfile
from pathlib import Path
request=json.load(sys.stdin)
child=r"""import contextlib,importlib.util,io,json,os,sys,uuid
from pathlib import Path
from unittest.mock import patch
request=json.load(sys.stdin);stdout=sys.stdout
try:
 with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
  from airflow import settings
  expected=str(Path(os.environ['TRAFFIC_SKIP_PROOF_HOME'])/'metadata.db')
  if settings.engine.url.get_backend_name()!='sqlite' or settings.engine.url.database!=expected:raise RuntimeError('Metadata DB isolation failed')
  from airflow.utils import db
  db.initdb()
  root=Path(os.environ['TRAFFIC_SKIP_PROOF_HOME'])
  for name,source in request['files'].items():(root/name).write_text(source)
  source=request['files']['dag.py'].replace('/opt/traffic-cloud/airflow_control.py',str(root/'airflow_control.py')).replace('/opt/traffic-cloud/legacy/traffic_region_training_worker.py',str(root/'legacy.py'))
  module=type(sys)('skip_contract_proof');sys.modules[module.__name__]=module;exec(compile(source,'skip_contract_proof.py','exec'),module.__dict__)
  module.control.request_stop=lambda conf:None
  expected_receipts={};finalized=[]
  def response(conf,branch):
   if conf.get('synthetic_failure') and branch=='tree':raise RuntimeError('Isolated synthetic task failure')
   receipt={'version':1,'region_id':conf['region_id'],'run_id':conf['training_run_id'],'branch':branch,'bundle_id':str(uuid.uuid4()),'manifest_sha256':'a'*64}
   expected_receipts[branch]=receipt
   chosen=any(name.endswith('_lag') for name in conf['selected_models']) if branch=='tree' else branch in conf['selected_models']
   return {'training_run_id':conf['training_run_id'],'branch':branch,'status':'ready' if chosen else 'skipped','artifact_receipt':receipt}
  def finalize(conf,receipts):
   if receipts!=expected_receipts or set(receipts)!={'tree','lstm','gru'}:raise RuntimeError('Skipped XCom receipt missing')
   finalized.append(True);return {'training_run_status':'completed'}
  module.control.train=response;module.control.finalize=finalize
  import pendulum
  cases=[]
  selections=[['random_forest_lag'],['gru'],['lstm','gru'],['random_forest_lag','xgboost_lag','lightgbm_lag','lstm','gru'],['random_forest_lag','lstm']]
  for index,selected in enumerate(selections):
   expected_receipts.clear();finalized.clear()
   conf={key:str(uuid.uuid4()) for key in ['region_id','dataset_id','training_run_id']}
   conf.update(_execution_protocol=2,training_backend='kaggle',selected_models=selected,train_start_date='2026-01-01',train_end_date='2026-02-15',synthetic_failure=index==len(selections)-1)
   with patch('requests.sessions.Session.request',side_effect=AssertionError('No external training/network allowed')):
    run=module.dag.test(execution_date=pendulum.datetime(2026,1,index+1,tz='UTC'),run_conf=conf)
   from airflow.utils.session import create_session
   with create_session() as session:states={ti.task_id:str(ti.state) for ti in run.get_task_instances(session=session)}
   if index==len(selections)-1:
    if states['train_tree_models']!='failed' or finalized:raise RuntimeError('Failure incorrectly reached finalize')
   else:
    expected_states={'train_tree_models':'success' if any(name.endswith('_lag') for name in selected) else 'skipped','train_lstm':'success' if 'lstm' in selected else 'skipped','train_gru':'success' if 'gru' in selected else 'skipped','finalize_region_training':'success'}
    if any(states[name]!=value for name,value in expected_states.items()) or not finalized:raise RuntimeError('Selected/Skipped/finalize state differs')
   cases.append({'selected_models':selected,'synthetic_failure':index==len(selections)-1,'states':states})
 out={'real_airflow_skip_xcom_verified':True,'isolated_sqlite_only':True,'business_db_s3_written':False,'fit_called':False,'cases':cases,'configmaps_sha256':request['configmaps_sha256']}
 stdout.write('TRAFFIC_SKIP_PROOF '+json.dumps(out)+'\n');stdout.flush()
except BaseException as error:
 import traceback
 frames=traceback.extract_tb(error.__traceback__)
 stdout.write('TRAFFIC_SKIP_PROOF '+json.dumps({'real_airflow_skip_xcom_verified':False,'error_type':type(error).__name__,'location':Path(frames[-1].filename).name+':'+str(frames[-1].lineno) if frames else None})+'\n');stdout.flush()
"""
with tempfile.TemporaryDirectory(prefix='traffic-airflow-skip-proof-') as directory:
 env={**os.environ,'TRAFFIC_SKIP_PROOF_HOME':directory,'AIRFLOW_HOME':directory,
      'AIRFLOW__DATABASE__SQL_ALCHEMY_CONN':'sqlite:///'+str(Path(directory)/'metadata.db'),
      'AIRFLOW__CORE__EXECUTOR':'SequentialExecutor','AIRFLOW__CORE__LOAD_EXAMPLES':'false',
      'AIRFLOW__CORE__LOAD_DEFAULT_CONNECTIONS':'false','AIRFLOW__CORE__DAGS_FOLDER':directory,
      'AIRFLOW__CORE__PLUGINS_FOLDER':str(Path(directory)/'plugins'),
      'AIRFLOW__LOGGING__REMOTE_LOGGING':'false','AIRFLOW__LOGGING__BASE_LOG_FOLDER':str(Path(directory)/'logs')}
 for key in ['AIRFLOW__DATABASE__SQL_ALCHEMY_CONN_CMD','AIRFLOW__DATABASE__SQL_ALCHEMY_CONN_SECRET','AIRFLOW__CORE__SQL_ALCHEMY_CONN']:env.pop(key,None)
 try:
  result=subprocess.run([sys.executable,'-B','-c',child],input=json.dumps(request),text=True,env=env,cwd=directory,capture_output=True,timeout=150)
  for line in result.stdout.splitlines():
   if line.startswith('TRAFFIC_SKIP_PROOF '):print(line)
 except BaseException as error:print('TRAFFIC_SKIP_PROOF '+json.dumps({'real_airflow_skip_xcom_verified':False,'error_type':type(error).__name__}))
'''


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--review", required=True)
    args = parser.parse_args()
    try:
        from manage_rollout import Rollout
        root = Path(__file__).resolve().parents[2]
        directory = Path(args.review)
        rollout = Rollout(args.kubectl, directory, directory / "baseline.json")
        resources = json.loads((directory / "configmaps.json").read_text(encoding="utf-8"))["items"]
        files = next(item["data"] for item in resources if item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
        command = [args.kubectl, "--request-timeout=170s", "-n", "traffic-mlops", "exec", "-i", rollout.pod("airflow-scheduler"),
                   "-c", "airflow-scheduler", "--", "python", "-B", "-c", REMOTE]
        result = subprocess.run(command, input=json.dumps({"files": files, "configmaps_sha256": rollout.review["files"]["configmaps.json"]}),
                                text=True, encoding="utf-8", capture_output=True, timeout=180)
        lines = [line.removeprefix("TRAFFIC_SKIP_PROOF ") for line in result.stdout.splitlines() if line.startswith("TRAFFIC_SKIP_PROOF ")]
        if len(lines) != 1:
            raise RuntimeError("Isolated Airflow did not return one proof.")
        proof = json.loads(lines[0])
        (directory / "airflow-skip-proof.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
        print(json.dumps(proof))
        raise SystemExit(0 if proof.get("real_airflow_skip_xcom_verified") else 2)
    except Exception as error:
        print(json.dumps({"real_airflow_skip_xcom_verified": False, "error_type": type(error).__name__, "raw_details_omitted": True}))
        raise SystemExit(2)
