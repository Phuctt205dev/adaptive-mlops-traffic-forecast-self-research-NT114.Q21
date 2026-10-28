"""Reload already verified branch artifacts on CPU. Never fit or publish."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"


def verify(items):
    import joblib
    import numpy as np
    import pandas as pd
    for item in items:
        path = Path(item["compatibility_file"])
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Compatibility samples are missing or exceed budget.")
        with np.load(path, allow_pickle=False) as data:
            features, expected = data["X"], data["expected"]
            if features.ndim not in {2, 3} or not 1 <= features.shape[0] <= 4 or not np.isfinite(features).all():
                raise ValueError("Invalid compatibility sample dimensions/values.")
            if item["model_family"] == "neural_sequence":
                import tensorflow as tf
                tf.config.set_visible_devices([], "GPU")
                model = tf.keras.models.load_model(item["model_file"], compile=False, safe_mode=True)
                actual = model.predict(features, verbose=0, batch_size=4)
                np.testing.assert_allclose(np.asarray(actual).reshape(-1), expected.reshape(-1), rtol=1e-4, atol=1e-5)
                preprocessor = joblib.load(item["preprocessor_file"])
                inverse = preprocessor["target_scaler"].inverse_transform(np.asarray(actual).reshape(-1, 1))
                np.testing.assert_allclose(inverse.reshape(-1), data["raw_expected"].reshape(-1), rtol=1e-4, atol=1e-3)
                tf.keras.backend.clear_session()
            elif item["model_family"] == "legacy_sklearn":
                columns = data["columns"]
                if columns.ndim != 1 or len(columns) != features.shape[1] or len(columns) > 256:
                    raise ValueError("Invalid tree compatibility columns.")
                model = joblib.load(item["model_file"])
                actual = model.predict(pd.DataFrame(features, columns=columns.tolist()))
                np.testing.assert_allclose(np.asarray(actual).reshape(-1), expected.reshape(-1), rtol=1e-4, atol=1e-5)
            else:
                raise ValueError("Unsupported model family for serving compatibility.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    try:
        from scripts.cloud_training.execute_branch import guard_parent
        guard_parent()
        verify(json.loads(Path(args.request).read_text(encoding="utf-8")))
        print('{"serving_compatibility_verified":true}', flush=True)
        return 0
    except Exception:
        print('{"serving_compatibility_verified":false}', flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
