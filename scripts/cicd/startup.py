"""Construct the checked candidate on each existing role before any rollout."""
import base64
import json


REMOTE = r'''import base64,importlib.util,json,os,sys,tempfile,traceback
from pathlib import Path
request=json.load(sys.stdin)
try:
 with tempfile.TemporaryDirectory(prefix="traffic-delivery-startup-") as directory:
  root=Path(directory)
  for name,value in request["files"].items():
   if name.startswith("/") or "\\" in name or any(p in {"",".",".."} for p in name.split("/")):raise ValueError("Unsafe source path")
   path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(base64.b64decode(value,validate=True))
  release_file=root/"checked-release.json";release_file.write_text(json.dumps(request["release"]))
  os.environ["TRAFFIC_CLOUD_MODE"]=request["mode"]
  os.environ["TRAINING_CONTROL_ENABLED"]="false"
  os.environ["CLOUD_TRAINING_ENABLED"]="false"
  spec=importlib.util.spec_from_file_location("delivery_candidate",root/"cloud_training_runtime.py")
  module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  app=module.build_app(root=root,release_file=release_file)
  paths=app.openapi()["paths"]
  expected="/api/v1/internal/training-runs/{run_id}/control/{branch}/start" if request["mode"]=="worker" else "/api/v1/admin/training-monitor/capabilities"
  if expected not in paths:raise RuntimeError("Required routes are missing")
  from fastapi.testclient import TestClient
  # Deliberately do not enter TestClient: app lifespan/bootstrap stays stopped.
  client=TestClient(app)
  try:
   denied=client.post("/api/v1/internal/training-runs/00000000-0000-0000-0000-000000000000/control/tree/start")
   wanted=503 if request["mode"]=="api" else 403
   if denied.status_code!=wanted:raise RuntimeError("Required execution/token gate is missing")
   prefix="/api/v1/health" if request["mode"]=="api" else "/health"
   response=client.get(prefix+"/release");response.raise_for_status();actual=response.json()
   if any(actual.get(key)!=request["release"][source] for key,source in (("commit","commit"),("release_id","release_id"),("bundle_id","cloud_bundle_id"))):raise RuntimeError("Candidate identity differs")
  finally:client.close()
  print("TRAFFIC_DELIVERY_STARTUP "+json.dumps({"source_startup_verified":True,"mode":request["mode"],"bundle_id":request["release"]["cloud_bundle_id"],"commit":request["release"]["commit"],"release_id":request["release"]["release_id"],"execution_gate_verified":True,"release_health_verified":True,"lifespan_not_started":True,"fit_called":False,"deployment_changed":False}))
except Exception as error:
 frames=traceback.extract_tb(error.__traceback__)
 print("TRAFFIC_DELIVERY_STARTUP "+json.dumps({"source_startup_verified":False,"error_type":type(error).__name__,"location":Path(frames[-1].filename).name+":"+str(frames[-1].lineno) if frames else "unknown"}))
 raise SystemExit(2)
'''


def check(cluster, directory, release, name, deployment, main):
    root = directory / "payload/cloud"
    files = {path.relative_to(root).as_posix(): base64.b64encode(path.read_bytes()).decode()
             for path in root.rglob("*") if path.is_file()}
    request = {"files": files, "mode": "api" if name == "traffic-api" else "worker", "release": release}
    pod = cluster.pod(deployment)
    # Stream a single prefixed proof even when candidate construction fails.
    # The wrapper keeps kubectl successful so sanitized diagnostics survive.
    wrapper = "import sys;source=" + repr(REMOTE) + ";\ntry: exec(compile(source,'candidate-startup-check','exec'))\nexcept SystemExit: pass"
    output = cluster.call("exec", "-i", pod, "-c", main["name"], "--", "python", "-B", "-c", wrapper,
                          value=request, timeout=180, json_output=False)
    proofs = [json.loads(line.removeprefix("TRAFFIC_DELIVERY_STARTUP ")) for line in output.splitlines()
              if line.startswith("TRAFFIC_DELIVERY_STARTUP ")]
    if len(proofs) != 1:
        raise RuntimeError("Candidate startup returned no unique proof: " + name)
    proof = proofs[0]
    required = ("source_startup_verified", "execution_gate_verified", "release_health_verified", "lifespan_not_started")
    if (any(proof.get(key) is not True for key in required) or proof.get("fit_called") is not False
            or proof.get("deployment_changed") is not False or proof.get("bundle_id") != release["cloud_bundle_id"]
            or proof.get("commit") != release["commit"] or proof.get("release_id") != release["release_id"]):
        raise RuntimeError("Candidate startup/gates failed before rollout: " + name)
    return {**proof, "deployment": name}
