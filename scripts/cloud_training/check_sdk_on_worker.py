"""Test the pinned SDK in a temporary isolated Linux venv, without credentials."""
import argparse
import json
from pathlib import Path
import subprocess

REMOTE = '''import json,os,subprocess,sys,tempfile,venv
from pathlib import Path
from urllib.request import urlopen
try:
 request=json.load(sys.stdin)
 with urlopen("http://127.0.0.1:8000/health/integration",timeout=3) as r:before=json.load(r)
 if before.get("busy"):raise RuntimeError("Worker is busy")
 with tempfile.TemporaryDirectory(prefix="traffic-kaggle-sdk-check-") as directory:
  root=Path(directory);(root/"scratch").mkdir()
  (root/"requirements.txt").write_text(request["requirements"])
  venv.create(root/"sdk",with_pip=True)
  python=str(root/"sdk/bin/python")
  env={**os.environ,"TMPDIR":str(root/"scratch"),"PYTHONDONTWRITEBYTECODE":"1"}
  subprocess.run([python,"-m","pip","--isolated","install","--no-cache-dir","--only-binary=:all:","--disable-pip-version-check","-r",str(root/"requirements.txt")],env=env,check=True,timeout=180,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  source="import importlib.metadata as m,json;from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest;print(json.dumps({x:m.version(x) for x in ['kaggle','kagglesdk']}))"
  result=subprocess.run([python,"-B","-c",source],env=env,check=True,timeout=20,capture_output=True,text=True)
  versions=json.loads(result.stdout)
 with urlopen("http://127.0.0.1:8000/health/integration",timeout=3) as r:after=json.load(r)
 if after.get("busy") or after.get("operation_count")!=before.get("operation_count"):raise RuntimeError("Worker changed during the check")
 print("TRAFFIC_SDK_CHECK "+json.dumps({"isolated_linux_sdk_verified":True,"versions":versions,"uid":os.getuid(),"credential_sent":False,"application_environment_changed":False,"fit_called":False}))
except Exception as error:
 print("TRAFFIC_SDK_CHECK "+json.dumps({"isolated_linux_sdk_verified":False,"error_type":type(error).__name__}))
 raise SystemExit(2)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--review", required=True)
    args = parser.parse_args()
    try:
        from prepare_rollout import SDK_LOCK
        command = [args.kubectl, "--request-timeout=30s", "-n", "traffic-mlops", "exec", "-i", args.pod,
                   "-c", "training-worker", "--", "python", "-B", "-c", REMOTE]
        result = subprocess.run(command, input=json.dumps({"requirements": SDK_LOCK}), text=True, encoding="utf-8",
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=230)
        lines = [line for line in result.stdout.splitlines() if line.startswith("TRAFFIC_SDK_CHECK ")]
        if len(lines) != 1:
            raise RuntimeError("SDK checker did not return one proof.")
        proof = json.loads(lines[0].removeprefix("TRAFFIC_SDK_CHECK "))
        (Path(args.review) / "sdk-linux-proof.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
        print(json.dumps(proof))
        return 0 if proof.get("isolated_linux_sdk_verified") else 2
    except Exception as error:
        print(json.dumps({"sdk_check_failed": True, "error_type": type(error).__name__, "raw_details_omitted": True}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
