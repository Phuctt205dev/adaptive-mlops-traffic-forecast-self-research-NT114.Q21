"""Reload our verified synthetic Kaggle output in an idle recurrent worker.

No fit, API serving process, DB, S3, registry, Kubernetes spec or image changes.
The short-lived Python verifier uses only a self-cleaning /tmp directory.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.cloud_training.probe import ProbeError, load_job, verify_output, write_json


REMOTE = r'''
import base64, hashlib, json, os, sys, tempfile, urllib.request, zipfile
from pathlib import Path
os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['TF_NUM_INTEROP_THREADS'] = '1'
os.environ['TF_NUM_INTRAOP_THREADS'] = '1'

def health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8000/health/integration', timeout=5) as response:
        data = json.loads(response.read(65537))
    if (data.get('role') != 'recurrent' or data.get('node') != 'mlops-node-03'
            or data.get('busy') is not False or data.get('runtime_revision') != 'branch-execution-v1'):
        raise ValueError('worker_not_idle_or_wrong_identity')
    return data

try:
    raw = sys.stdin.buffer.read(12 * 1024 * 1024 + 1)
    if len(raw) > 12 * 1024 * 1024:
        raise ValueError('payload_too_large')
    payload = json.loads(raw)
    before = health()
    if before.get('pod') != payload['pod']:
        raise ValueError('worker_pod_mismatch')
    files = payload['files']
    if set(files) != {'model.keras', 'samples.npz', 'preprocessor.json'}:
        raise ValueError('file_contract_mismatch')
    with tempfile.TemporaryDirectory(prefix='traffic-kaggle-cpu-probe-') as temp:
        root = Path(temp)
        total = 0
        for name, metadata in files.items():
            data = base64.b64decode(metadata['base64'], validate=True)
            total += len(data)
            if (len(data) != metadata['size'] or total > 8 * 1024 * 1024
                    or hashlib.sha256(data).hexdigest() != metadata['sha256']):
                raise ValueError('file_integrity_mismatch')
            (root / name).write_bytes(data)
        if json.loads((root / 'preprocessor.json').read_text()) != {
            'schema':1, 'kind':'identity', 'sequence_length':6, 'features':2}:
            raise ValueError('preprocessing_mismatch')
        with zipfile.ZipFile(root / 'samples.npz') as archive:
            if sum(item.file_size for item in archive.infolist()) > 1024 * 1024:
                raise ValueError('expanded_samples_too_large')
        import numpy as np
        import tensorflow as tf
        if tf.__version__ != '2.21.0':
            raise ValueError('expected_tensorflow_2_21_0')
        tf.config.set_visible_devices([], 'GPU')
        model = tf.keras.models.load_model(root / 'model.keras', safe_mode=True, compile=False)
        with np.load(root / 'samples.npz', allow_pickle=False) as samples:
            values, expected = samples['inputs'], samples['predictions']
            if values.shape != (4,6,2) or expected.shape != (4,1):
                raise ValueError('sample_shape_mismatch')
            actual = model(values, training=False).numpy()
        if not np.isfinite(actual).all() or not np.allclose(actual, expected, rtol=1e-4, atol=1e-5):
            raise ValueError('prediction_mismatch')
        max_error = float(np.max(np.abs(actual-expected)))
        del model
        tf.keras.backend.clear_session()
    after = health()
    if after.get('pod') != before.get('pod') or after.get('operation_count') != before.get('operation_count'):
        raise ValueError('worker_operations_changed_during_probe')
    proof = {'cpu_reload_verified':True, 'tensorflow':tf.__version__, 'samples_compared':4,
             'maximum_absolute_error':max_error, 'rtol':1e-4, 'atol':1e-5,
             'worker_pod':before['pod'], 'worker_node':before['node'],
             'worker_operations_unchanged':True, 'temporary_files_removed':True,
             'fit_called':False, 'business_data_written':False, 'production_ready':False}
except Exception as error:
    proof = {'cpu_reload_verified':False, 'error_type':type(error).__name__,
             'message':'Synthetic CPU verification failed; raw worker details omitted.', 'production_ready':False}
print('TRAFFIC_CPU_PROBE ' + json.dumps(proof), flush=True)
sys.exit(0 if proof['cpu_reload_verified'] else 2)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--pod", required=True)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"traffic-training-worker-[a-z0-9]+-[a-z0-9]+", args.pod):
            raise ProbeError("Only an explicit recurrent training worker can be used for this probe.")
        directory, job = load_job(args.job)
        local_proof = verify_output(directory / "output", job)
        files = {}
        for name in ("model.keras", "samples.npz", "preprocessor.json"):
            data = (directory / "output" / name).read_bytes()
            files[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                           "base64": base64.b64encode(data).decode("ascii")}
        cli = Path(os.environ["LOCALAPPDATA"]) / "Programs/kubectl/kubectl.exe"
        if not cli.is_file():
            raise ProbeError("The reviewed kubectl installation is missing.")
        command = [str(cli), "--request-timeout=20s", "-n", "traffic-mlops", "exec", "-i", args.pod,
                   "--", "python", "-B", "-c", REMOTE]
        result = subprocess.run(command, input=json.dumps({"pod": args.pod, "files": files}),
                                capture_output=True, text=True, timeout=85)
        lines = [line for line in result.stdout.splitlines() if line.startswith("TRAFFIC_CPU_PROBE ")]
        if len(lines) != 1 or len(lines[0]) > 8192:
            raise ProbeError("The worker did not return one bounded verification proof; raw logs omitted.")
        proof = json.loads(lines[0].split(" ", 1)[1])
        if result.returncode or proof.get("cpu_reload_verified") is not True:
            raise ProbeError("The worker did not verify the synthetic model on CPU. No production readiness claimed.")
        report = {**local_proof, **proof, "job_id": job["job_id"]}
        write_json(directory / "verification.json", report)
        print(json.dumps(report, allow_nan=False))
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, ProbeError) else "CPU probe failed or timed out; completion is unconfirmed."
        print(json.dumps({"error": message, "cpu_reload_verified": False, "production_ready": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
