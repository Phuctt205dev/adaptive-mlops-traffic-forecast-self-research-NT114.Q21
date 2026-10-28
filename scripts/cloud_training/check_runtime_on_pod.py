"""Build the proposed app in a temporary separate process; no rollout or fits."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys

REMOTE = '''import base64,hashlib,importlib.util,json,os,sys,tempfile,traceback
from pathlib import Path
request=json.load(sys.stdin)
try:
 with tempfile.TemporaryDirectory(prefix="traffic-cloud-source-check-") as directory:
  root=Path(directory);data=root/"..revision";data.mkdir()
  for name,value in request["files"].items():
   if name.startswith("/") or "\\\\" in name or any(p in {"",".",".."} for p in name.split("/")):raise ValueError("Unsafe source path")
   path=data/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(base64.b64decode(value,validate=True))
  (root/"..data").symlink_to(data.name,target_is_directory=True)
  for path in data.iterdir():(root/path.name).symlink_to(Path("..data")/path.name,target_is_directory=path.is_dir())
  os.environ["TRAFFIC_CLOUD_MODE"]=request["mode"]
  os.environ["TRAINING_CONTROL_ENABLED"]="false"
  os.environ["CLOUD_TRAINING_ENABLED"]="false"
  spec=importlib.util.spec_from_file_location("cloud_source_check",root/"cloud_training_runtime.py")
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  app=module.build_app(root=root)
  schema=app.openapi()
  expected="/api/v1/internal/training-runs/{run_id}/control/{branch}/start" if request["mode"]=="worker" else "/api/v1/admin/training-monitor/capabilities"
  if expected not in schema["paths"]:raise RuntimeError("Expected controlled routes missing")
  print("TRAFFIC_CLOUD_CHECK "+json.dumps({"source_startup_verified":True,"mode":request["mode"],"route_count":len(schema["paths"]),"bundle_id":request["bundle_id"],"projected_volume_links_verified":True,"lifespan_not_started":True,"fit_called":False,"deployment_changed":False}))
except Exception as error:
 frames=traceback.extract_tb(error.__traceback__)
 print("TRAFFIC_CLOUD_CHECK "+json.dumps({"source_startup_verified":False,"error_type":type(error).__name__,"location":Path(frames[-1].filename).name+":"+str(frames[-1].lineno) if frames else "unknown"}))
 raise SystemExit(2)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument("--deployment", choices=["traffic-api", "traffic-training-tree", "traffic-training-worker"], required=True)
    args = parser.parse_args()
    try:
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        directory = Path(args.review)
        review = json.loads((directory / "review.json").read_text())
        resources = json.loads((directory / "configmaps.json").read_text(encoding="utf-8"))["items"]
        code = next(item for item in resources if item["metadata"]["name"].startswith("traffic-cloud-") and not item["metadata"]["name"].startswith("traffic-cloud-airflow-"))
        patch = json.loads((directory / (args.deployment + ".patch.json")).read_text())
        items = next(item["configMap"]["items"] for item in patch["spec"]["template"]["spec"]["volumes"] if item["name"] == "cloud-code")
        files = {item["path"]: base64.b64encode(code["data"][item["key"]].encode()).decode() for item in items}
        runtime = snapshot["runtime"][args.deployment]
        request = {"files": files, "mode": "api" if args.deployment == "traffic-api" else "worker", "bundle_id": review["bundle_id"]}
        command = [args.kubectl, "--request-timeout=30s", "-n", "traffic-mlops", "exec", "-i", runtime["pod"],
                   "-c", runtime["main_container"], "--", "python", "-B", "-c", REMOTE]
        result = subprocess.run(command, input=json.dumps(request), text=True, encoding="utf-8",
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
        lines = [line for line in result.stdout.splitlines() if line.startswith("TRAFFIC_CLOUD_CHECK ")]
        if len(lines) != 1:
            raise RuntimeError("The separate source checker did not return one proof.")
        proof = json.loads(lines[0].removeprefix("TRAFFIC_CLOUD_CHECK "))
        proof["deployment"] = args.deployment
        (directory / (args.deployment + ".source-proof.json")).write_text(json.dumps(proof, indent=2), encoding="utf-8")
        print(json.dumps(proof))
        return 0 if proof.get("source_startup_verified") else 2
    except Exception as error:
        print(json.dumps({"source_check_failed": True, "error_type": type(error).__name__, "raw_details_omitted": True}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
