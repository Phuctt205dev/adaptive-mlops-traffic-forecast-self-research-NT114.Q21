"""CPU executor image check. Offline artifact loading/inference, never fitting."""
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys


def check(directory):
    metadata = json.loads(Path('/app/executor-release.json').read_bytes())
    identity = {k: v for k, v in metadata.items() if k not in {'source_id', 'commit'}}
    assert hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == metadata['source_id']
    for name, wanted in metadata['files'].items():
        if name == 'Dockerfile':
            continue
        assert hashlib.sha256((Path('/app') / name).read_bytes()).hexdigest() == wanted
    subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)
    for name in ('sklearn', 'xgboost', 'lightgbm', 'tensorflow', 'shap', 'mlflow', 'boto3', 'src.local_training.job'):
        importlib.import_module(name)
    import joblib
    import numpy as np
    import pandas as pd
    import tensorflow as tf
    tf.config.set_visible_devices([], 'GPU')
    fixture = json.loads((directory / 'expected.json').read_bytes())
    for name, wanted in fixture['files'].items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == wanted
    tested = []
    for name, entry in fixture['models'].items():
        if entry['family'] == 'tree':
            model = joblib.load(directory / entry['file'])
            values = pd.DataFrame(np.load(directory / 'tree-input.npy', allow_pickle=False), columns=['a', 'b', 'c'])
            actual = model.predict(values)
        else:
            model = tf.keras.models.load_model(directory / entry['file'], compile=False, safe_mode=True)
            actual = model(np.load(directory / 'neural-input.npy', allow_pickle=False), training=False).numpy().reshape(-1)
            tf.keras.backend.clear_session()
        np.testing.assert_allclose(actual, entry['expected'], rtol=1e-5, atol=1e-5)
        tested.append(name)
    assert len(tested) == 5
    print(json.dumps({'role': metadata['role'], 'source_id': metadata['source_id'], 'models': tested,
                      'offline': True, 'training_started': False, 'provider_called': False}))


if __name__ == '__main__':
    check(Path(sys.argv[1]))
