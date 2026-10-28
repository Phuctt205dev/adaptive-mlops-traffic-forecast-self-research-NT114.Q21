"""CPU-only cancellation capability test using this private notebook's source.

The controller Quick Saves an exact cancel marker, without starting a new run.
No external callback, embedded credential or internal system access is used.
"""
CONFIG = {}  # populated by prepare_probe


def main():
    import contextlib
    import io
    import json
    import os
    from pathlib import Path
    import signal
    import subprocess
    import sys
    import time

    sequence = 0
    child = None
    child_source = Path("/kaggle/working/traffic_cancel_probe_child.py")

    def emit(stage):
        nonlocal sequence
        sequence += 1
        print("TRAFFIC_TRAINING_EVENT " + json.dumps({
            "schema": 1, "job_id": CONFIG["job_id"], "seq": sequence,
            "stage": stage, "model": "ProcessProbe",
        }), flush=True)

    def stop_child():
        if child is None or child.poll() is not None:
            return
        # Only signal the process group we created for this probe.
        if os.getpgid(child.pid) != child.pid:
            raise RuntimeError("Probe child process identity differs.")
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)

    emit("starting")
    try:
        # Kaggle injects its own notebook-scoped API credential. Never print
        # environment values or attach the user's local token to this source.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from kaggle.api.kaggle_api_extended import KaggleApi
            from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest
            api = KaggleApi()
            api.authenticate()
        owner, slug = CONFIG["reference"].split("/")
        marker = "# TRAFFIC_POC_CANCEL " + CONFIG["job_id"]

        def cancel_signal():
            request = ApiGetKernelRequest()
            request.user_name, request.kernel_slug = owner, slug
            with api.build_kaggle_client() as client:
                remote = client.kernels.kernels_api_client.get_kernel(request)
            if remote.metadata.ref != CONFIG["reference"] or remote.metadata.is_private is not True:
                raise RuntimeError("Probe control identity/privacy differs.")
            return marker in remote.blob.source.splitlines()

        if cancel_signal():
            emit("cancelled")
            return 0
        emit("control_ready")
        # Repeated small native fits prove that the worker child is stoppable.
        # It creates no model/checkpoint artifacts and has its own hard deadline.
        child_source.write_text(
            "import os,time\n"
            "os.environ['OMP_NUM_THREADS']='1'\n"
            "os.environ['OPENBLAS_NUM_THREADS']='1'\n"
            "import numpy as np\n"
            "from sklearn.ensemble import RandomForestRegressor\n"
            "rng=np.random.default_rng(42)\n"
            "x=rng.normal(size=(1000,8)); y=x.sum(axis=1)\n"
            "deadline=time.monotonic()+120\n"
            "while time.monotonic()<deadline:\n"
            " RandomForestRegressor(n_estimators=20,max_depth=8,n_jobs=1,random_state=42).fit(x,y)\n",
            encoding="utf-8",
        )
        child = subprocess.Popen([sys.executable, str(child_source)], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 110
        while time.monotonic() < deadline:
            if cancel_signal():
                stop_child()
                if child.poll() is None:
                    raise RuntimeError("The child did not stop.")
                emit("cancelled")
                return 0
            if child.poll() is not None:
                raise RuntimeError("The probe child exited before cancellation.")
            emit("child_running")
            time.sleep(3)
        emit("cancel_not_observed")
        return 2
    except Exception:
        # Do not expose SDK/authentication internals to notebook logs.
        emit("failed")
        return 2
    finally:
        stop_child()
        if child_source.is_file() and not child_source.is_symlink():
            child_source.unlink()


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
