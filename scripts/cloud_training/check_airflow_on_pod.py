"""Import reviewed DAG sources in a temporary Airflow process; never run tasks."""
import argparse
import json
from pathlib import Path
import sys
import subprocess

REMOTE = '''import contextlib,hashlib,importlib.util,io,json,sys,tempfile,traceback
from pathlib import Path
from unittest.mock import patch
request=json.load(sys.stdin)
try:
 with tempfile.TemporaryDirectory(prefix="traffic-cloud-dag-check-") as directory, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
  root=Path(directory)
  for name,source in request["files"].items():(root/name).write_text(source,encoding="utf-8")
  def load(name,source):
   spec=importlib.util.spec_from_loader(name,loader=None);module=importlib.util.module_from_spec(spec)
   sys.modules[name]=module;exec(compile(source,name,"exec"),module.__dict__);return module
  with patch("requests.sessions.Session.request",side_effect=AssertionError("Import cannot contact training services")):
   reference=load("dag_reference_check",request["files"]["legacy.py"])
   source=request["files"]["dag.py"].replace("/opt/traffic-cloud/airflow_control.py",str(root/"airflow_control.py")).replace("/opt/traffic-cloud/legacy/traffic_region_training_worker.py",str(root/"legacy.py"))
   proposed=load("dag_proposed_check",source)
  def graph(dag):return {t.task_id:sorted(t.downstream_task_ids) for t in dag.tasks}
  expected={"validate_training_conf":["train_gru","train_lstm","train_tree_models"],
            "train_tree_models":["finalize_region_training"],"train_lstm":["finalize_region_training"],
            "train_gru":["finalize_region_training"],"finalize_region_training":[]}
  if graph(proposed.dag)!=expected or reference.dag.dag_id!=proposed.dag.dag_id or proposed.dag.max_active_runs!=1 or proposed.dag.max_active_tasks!=3:raise RuntimeError("Controlled DAG concurrency contract differs")
  if len(proposed.dag.tasks)!=5 or not all(t.on_failure_callback==proposed.failed for t in proposed.dag.tasks):raise RuntimeError("Failure callback missing")
  # Airflow task execution uses DagBag, not module.dag. Importing a helper
  # containing another DAG can auto-register the same ID and overwrite it.
  from airflow.models import DagBag
  class CheckedBag(DagBag):
   def __init__(self,*args,**kwargs):
    self.candidates=[];super().__init__(*args,**kwargs)
   def bag_dag(self,dag,root_dag):
    if dag.dag_id=='traffic_region_training':self.candidates.append(dag)
    return super().bag_dag(dag,root_dag)
  path=root/'traffic_region_training.py';path.write_text(source,encoding='utf-8')
  expected_calls={'validate_training_conf':'validate_cloud','train_tree_models':'tree','train_lstm':'recurrent','train_gru':'recurrent','finalize_region_training':'finalize'}
  for _ in range(8):
   with patch('requests.sessions.Session.request',side_effect=AssertionError('Import cannot contact training services')):
    bag=CheckedBag(dag_folder=str(path),include_examples=False)
   loaded=bag.dags.get('traffic_region_training')
   if bag.import_errors or len(bag.candidates)!=1 or loaded is None:raise RuntimeError('Ambiguous DagBag registration')
   if graph(loaded)!=expected or loaded.max_active_tasks!=3:raise RuntimeError('DagBag graph differs')
   if {t.task_id:t.python_callable.__name__ for t in loaded.tasks}!=expected_calls:raise RuntimeError('DagBag selected legacy callables')
   if not all(getattr(t.on_failure_callback,'__name__',None)=='failed' for t in loaded.tasks):raise RuntimeError('DagBag failure callback missing')
  proposed.control.configuration=lambda context:context["conf"]
  proposed.control.train=lambda conf,branch:{"status":"ready","route":"controlled:"+branch}
  stop_requests=[];proposed.control.request_stop=lambda conf:stop_requests.append(conf)
  for invalid in ({},{'_execution_protocol':2,'training_backend':'local'},{'_execution_protocol':1,'training_backend':'kaggle'}):
   try:proposed.tree(conf=invalid)
   except ValueError:pass
   else:raise RuntimeError('Local/legacy dispatch is still available')
   proposed.failed({'conf':invalid})
  if stop_requests:raise RuntimeError('Invalid local conf attempted to stop an execution')
  if proposed.tree(conf={'_execution_protocol':2,'training_backend':'kaggle'})['route']!='controlled:tree':raise RuntimeError('Cloud dispatch differs')
  skip_contract=False
  if hasattr(proposed,'complete_or_skip'):
   import uuid
   from airflow.exceptions import AirflowSkipException
   if proposed.dag.get_task('train_gru').trigger_rule!='none_failed' or proposed.dag.get_task('finalize_region_training').trigger_rule!='none_failed':raise RuntimeError('Skipped tasks would block downstream work')
   for selected in [['random_forest_lag'],['gru'],['lstm','gru']]:
    conf={name:str(uuid.uuid4()) for name in ['region_id','dataset_id','training_run_id']};conf.update(_execution_protocol=2,training_backend='kaggle',selected_models=selected)
    stored={};expected={}
    class TI:
     def __init__(self,task):self.task=task
     def xcom_push(self,key,value):stored[self.task]=value
     def xcom_pull(self,task_ids):return stored[task_ids]
    def response(conf,branch):
     receipt={'version':1,'region_id':conf['region_id'],'run_id':conf['training_run_id'],'branch':branch,'bundle_id':str(uuid.uuid4()),'manifest_sha256':'a'*64}
     expected[branch]=receipt
     chosen=any(name.endswith('_lag') for name in selected) if branch=='tree' else branch in selected
     return {'training_run_id':conf['training_run_id'],'branch':branch,'status':'ready' if chosen else 'skipped','artifact_receipt':receipt}
    proposed.control.train=response
    for branch,task in proposed.control.TASKS.items():
     ti=TI(task)
     try:
      value=proposed.tree(conf=conf,ti=ti) if branch=='tree' else proposed.recurrent(branch.upper(),conf=conf,ti=ti)
      stored[task]=value
      if value['status']=='skipped':raise RuntimeError('Skipped response became Success')
     except AirflowSkipException:
      if task not in stored or stored[task]['status']!='skipped':raise RuntimeError('Skipped receipt lost')
    proposed.control.finalize=lambda conf,receipts:receipts
    if proposed.finalize(conf=conf,ti=TI('finalize'))!=expected:raise RuntimeError('Finalize lost a skipped receipt')
   skip_contract=True
  proof={"airflow_source_verified":True,"dag_id":proposed.dag.dag_id,"task_graph":graph(proposed.dag),"local_dispatch_blocked":True,"task_executed":False,"deployment_changed":False,"configmaps_sha256":request["configmaps_sha256"]}
  proof['skipped_receipts_verified']=skip_contract
  proof['real_dagbag_verified']=True;proof['dagbag_loads']=8;proof['dag_candidates']=1
 print("TRAFFIC_AIRFLOW_CHECK "+json.dumps(proof))
except Exception as error:
 frames=traceback.extract_tb(error.__traceback__)
 print("TRAFFIC_AIRFLOW_CHECK "+json.dumps({"airflow_source_verified":False,"error_type":type(error).__name__,"location":Path(frames[-1].filename).name+":"+str(frames[-1].lineno) if frames else "unknown"}))
 raise SystemExit(2)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument("--snapshot", required=True)
    args = parser.parse_args()
    try:
        from manage_rollout import Rollout
        rollout = Rollout(args.kubectl, args.review, args.snapshot)
        resources = json.loads((rollout.root / "configmaps.json").read_text(encoding="utf-8"))["items"]
        files = next(item["data"] for item in resources if item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
        command = [args.kubectl, "--request-timeout=60s", "-n", "traffic-mlops", "exec", "-i", rollout.pod("airflow-scheduler"),
                   "-c", "airflow-scheduler", "--", "python", "-B", "-c", REMOTE]
        result = subprocess.run(command, input=json.dumps({"files": files, "configmaps_sha256": rollout.review["files"]["configmaps.json"]}),
                                text=True, encoding="utf-8", capture_output=True, timeout=90)
        lines = [line.removeprefix("TRAFFIC_AIRFLOW_CHECK ") for line in result.stdout.splitlines() if line.startswith("TRAFFIC_AIRFLOW_CHECK ")]
        if len(lines) != 1:
            raise RuntimeError("Airflow checker did not return a single proof.")
        proof = json.loads(lines[0])
        (rollout.root / "airflow-source-proof.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
        print(json.dumps(proof))
        return 0 if proof.get("airflow_source_verified") else 2
    except Exception as error:
        print(json.dumps({"airflow_source_verified": False, "error_type": type(error).__name__, "raw_details_omitted": True}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
