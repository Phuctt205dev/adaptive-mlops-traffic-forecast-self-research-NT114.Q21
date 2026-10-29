"""Verify proposed entrypoints with temporary source/dependencies; never rollout."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.shap.check_neural_compatibility import REMOTE as COMPATIBILITY
from scripts.shap.rollout_neural_shap import NeuralRollout, code_manifest, read

REMOTE = r'''
import base64,contextlib,importlib.util,io,json,os,subprocess,sys,tempfile,traceback
from pathlib import Path
request=json.load(sys.stdin)
try:
 with tempfile.TemporaryDirectory(prefix="neural-shap-startup-") as directory:
  temporary=Path(directory);root=temporary/"source";data=root/"..revision";data.mkdir(parents=True)
  for name,encoded in request["files"].items():
   if name.startswith("/") or "\\" in name or any(x in {"",".",".."} for x in name.split("/")):raise ValueError("Source path invalid")
   target=data/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(base64.b64decode(encoded,validate=True))
  (root/"..data").symlink_to(data.name,target_is_directory=True)
  for path in data.iterdir():(root/path.name).symlink_to(Path("..data")/path.name,target_is_directory=path.is_dir())
  os.environ["TRAFFIC_CLOUD_MODE"]=request["mode"]
  os.environ["TRAINING_CONTROL_ENABLED"]="false";os.environ["CLOUD_TRAINING_ENABLED"]="false"
  extra={};compatibility=None
  if request["role"]=="recurrent":
   dependency_root=temporary/"shap";dependency_root.mkdir()
   for name,source in request["dependency_config"]["data"].items():
    if Path(name).name!=name:raise ValueError("Dependency source path invalid")
    (dependency_root/name).write_text(source,encoding="utf-8")
   sys.path.insert(0,str(dependency_root))
   spec=importlib.util.spec_from_file_location("neural_dependency_installer",dependency_root/"shap_install_dependencies.py")
   installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
   dependency_target=temporary/"dependencies"
   with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    installer.install(dependency_root,dependency_target)
   extra={"dependency_root":dependency_root,"dependency_target":dependency_target}
   environment=dict(os.environ);environment["PYTHONPATH"]=str(dependency_target)
   compat_request={"worker":True,"sources":request["compat_sources"]}
   result=subprocess.run([sys.executable,"-B","-c",request["compat_program"]],input=json.dumps(compat_request),
     encoding="utf-8",capture_output=True,env=environment,timeout=120)
   lines=[line.removeprefix("NEURAL_SHAP_PROOF ") for line in result.stdout.splitlines() if line.startswith("NEURAL_SHAP_PROOF ")]
   if result.returncode or len(lines)!=1:raise RuntimeError("Neural compatibility child failed")
   compatibility=json.loads(lines[0])
   if not compatibility.get("passed"):raise RuntimeError("Neural compatibility did not pass")
  spec=importlib.util.spec_from_file_location("neural_cloud_startup",root/"cloud_training_runtime.py")
  runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
  app=runtime.build_app(root=root,**extra)
  schema=app.openapi()
  expected="/api/v1/internal/training-runs/{run_id}/control/{branch}/start" if request["mode"]=="worker" else "/api/v1/admin/regions/{region_id}/feature-importance"
  if expected not in schema["paths"]:raise RuntimeError("Expected route absent")
  print("NEURAL_RUNTIME_PROOF "+json.dumps({"source_startup_verified":True,"bundle_id":request["bundle_id"],
    "role":request["role"],"route_count":len(schema["paths"]),"neural_compatibility":compatibility,
    "lifespan_not_started":True,"fit_called":False,"deployment_changed":False,"business_data_used":False}))
except Exception as error:
 frames=traceback.extract_tb(error.__traceback__)
 print("NEURAL_RUNTIME_PROOF "+json.dumps({"source_startup_verified":False,"error_type":type(error).__name__,
   "location":Path(frames[-1].filename).name+":"+str(frames[-1].lineno) if frames else None}))
 raise SystemExit(2)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--deployment", required=True, choices=["traffic-api", "traffic-training-tree", "traffic-training-worker"])
    args = parser.parse_args()
    operator = NeuralRollout(args.kubectl, args.directory)
    resources = read(operator.directory / "configmaps.json")["items"]
    code = next(item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-")
                and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
    manifest = code_manifest(code)
    # Read mapping from the exact proposed ConfigMap volume.
    patch = read(operator.directory / (args.deployment + ".patch.json"))
    items = next(item["configMap"]["items"] for item in patch["spec"]["template"]["spec"]["volumes"] if item["name"] == "cloud-code")
    role = "api" if args.deployment == "traffic-api" else "tree" if args.deployment.endswith("tree") else "recurrent"
    request = {"files": {item["path"]: base64.b64encode(code["data"][item["key"]].encode()).decode() for item in items},
               "mode": "api" if role == "api" else "worker", "role": role, "bundle_id": manifest["bundle_id"]}
    if role == "recurrent":
        request.update(dependency_config=read(operator.directory / "shap-dependency-config.json"), compat_program=COMPATIBILITY,
            compat_sources={name: base64.b64encode((ROOT / source).read_bytes()).decode() for name, source in {
                "neural_shap_check": "src/shap_reporting.py", "recurrent_check": "src/models/recurrent.py"}.items()})
    pod = operator.cluster.pod(args.deployment)
    container = operator.baseline["runtime"][args.deployment]["main_container"]
    result = subprocess.run([args.kubectl, "--request-timeout=240s", "-n", "traffic-mlops", "exec", "-i", pod,
        "-c", container, "--", "python", "-B", "-c", REMOTE], input=json.dumps(request), encoding="utf-8",
        capture_output=True, timeout=270)
    lines = [line.removeprefix("NEURAL_RUNTIME_PROOF ") for line in result.stdout.splitlines() if line.startswith("NEURAL_RUNTIME_PROOF ")]
    if len(lines) != 1:
        print(json.dumps({"source_startup_verified": False, "raw_details_omitted": True})); return 2
    proof = json.loads(lines[0])
    proof.update(deployment=args.deployment)
    (operator.directory / (args.deployment + ".source-proof.json")).write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps(proof))
    return 0 if result.returncode == 0 and proof.get("source_startup_verified") else 2


if __name__ == "__main__":
    raise SystemExit(main())
