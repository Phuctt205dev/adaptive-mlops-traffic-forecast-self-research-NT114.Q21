"""Self-contained synthetic notebook source. Only executed after explicit submit.

It never reads project data or contacts the project's API, database or storage.
"""
CONFIG = {}  # populated by prepare_probe


def main():
    import hashlib
    import json
    import os
    from pathlib import Path
    import time

    sequence = 0

    def emit(stage, **details):
        nonlocal sequence
        sequence += 1
        print("TRAFFIC_TRAINING_EVENT " + json.dumps({
            "schema": 1, "job_id": CONFIG["job_id"], "seq": sequence,
            "stage": stage, "model": "LSTM", **details,
        }, allow_nan=False), flush=True)

    emit("starting")
    try:
        # Do not dump environment variables: Kaggle may inject credentials.
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        import numpy as np
        import tensorflow as tf
        from tensorflow import keras

        tf.keras.utils.set_random_seed(42)
        gpu_count = len(tf.config.list_physical_devices("GPU"))
        if CONFIG["accelerator"] == "gpu" and not gpu_count:
            raise RuntimeError("A GPU was requested but not allocated.")
        emit("device_ready", gpu_count=gpu_count)
        if CONFIG["purpose"] == "cancel":
            # Bounded, idle cancellation target. No model artifacts are created.
            for _ in range(36):
                emit("waiting_for_cancel")
                time.sleep(5)
            emit("cancel_not_observed")
            raise RuntimeError("Cancellation was not observed within the probe budget.")

        # Small artificial sequence; no dataset upload and no production fit.
        rng = np.random.default_rng(42)
        x = rng.normal(size=(64, 6, 2)).astype("float32")
        y = x[:, -1].sum(axis=1).astype("float32")
        model = keras.Sequential([keras.Input((6, 2)), keras.layers.LSTM(4), keras.layers.Dense(1)])
        model.compile(optimizer="adam", loss="mse")
        for epoch in range(1, 4):
            emit("epoch_started", epoch=epoch, epochs=3)
            losses = [float(model.train_on_batch(x[start:start + 16], y[start:start + 16]))
                      for start in range(0, len(x), 16)]
            emit("epoch_completed", epoch=epoch, epochs=3, loss=float(np.mean(losses)))
            # Explicit observation window for the synthetic infrastructure
            # probe only. This sleep is not reported as model fit time.
            time.sleep(CONFIG.get("observation_seconds", 0) / 3)
        emit("saving")
        root = Path("/kaggle/working")
        root.mkdir(parents=True, exist_ok=True)
        model.save(root / "model.keras")
        # Explicit no-op preprocessing contract for this synthetic probe only.
        (root / "preprocessor.json").write_text(json.dumps({
            "schema": 1, "kind": "identity", "sequence_length": 6, "features": 2,
        }), encoding="utf-8")
        expected = model(x[:4], training=False).numpy()
        np.savez(root / "samples.npz", inputs=x[:4], predictions=expected)
        (root / "report.json").write_text(json.dumps({
            "schema": 1, "job_id": CONFIG["job_id"], "status": "completed",
            "gpu_count": gpu_count, "tensorflow": tf.__version__, "numpy": np.__version__,
            "synthetic_only": True, "epochs": 3,
        }), encoding="utf-8")
        files = []
        for name in ("model.keras", "preprocessor.json", "samples.npz", "report.json"):
            path = root / name
            files.append({"path": name, "size": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        # Manifest is the final commit marker, never written on a failure.
        (root / "manifest.json").write_text(json.dumps({
            "schema": 1, "job_id": CONFIG["job_id"], "files": files,
        }), encoding="utf-8")
        emit("completed")
    except Exception:
        emit("failed")
        raise


if __name__ == "__main__":
    main()
