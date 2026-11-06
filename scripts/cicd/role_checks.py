"""Offline producer/consumer artifact checks for every role image."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import subprocess


def fixtures(directory):
    import joblib
    import numpy as np
    import pandas as pd
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.preprocessing import StandardScaler
    from xgboost import XGBRegressor
    from lightgbm import LGBMRegressor
    directory.mkdir(parents=True, exist_ok=False)
    x = np.arange(48, dtype=float).reshape(16, 3) / 17
    y = np.sin(x[:, 0]) * 10 + x[:, 1]
    frame = pd.DataFrame(x, columns=['a', 'b', 'c'])
    np.save(directory / 'tree-input.npy', x, allow_pickle=False)
    models = {
        'random_forest': RandomForestRegressor(n_estimators=3, max_depth=2, random_state=42, n_jobs=1),
        'xgboost': XGBRegressor(n_estimators=3, max_depth=2, random_state=42, n_jobs=1, device='cpu'),
        'lightgbm': LGBMRegressor(n_estimators=3, max_depth=2, min_child_samples=1, verbosity=-1, n_jobs=1),
    }
    result = {'models': {}, 'files': {}}
    for name, model in models.items():
        model.fit(frame, y)  # Tiny offline fixture; never a product training run.
        joblib.dump(model, directory / (name + '.joblib'))
        expected=model.predict(frame)
        np.savez(directory/(name+'.npz'),X=x[:4],expected=expected[:4],columns=np.array(frame.columns,dtype=str))
        result['models'][name] = {'family': 'tree', 'file': name + '.joblib', 'expected': expected.tolist(), 'compatibility':name+'.npz'}
    scaler=StandardScaler().fit(y.reshape(-1, 1))
    joblib.dump({'target_scaler':scaler}, directory / 'preprocessor.joblib')
    import tensorflow as tf
    tf.config.set_visible_devices([], 'GPU')
    tf.keras.utils.set_random_seed(42)
    x_neural = np.arange(24, dtype='float32').reshape(4, 3, 2) / 23
    np.save(directory / 'neural-input.npy', x_neural, allow_pickle=False)
    for name, layer in [('lstm', tf.keras.layers.LSTM), ('gru', tf.keras.layers.GRU)]:
        model = tf.keras.Sequential([tf.keras.layers.Input((3, 2)), layer(2), tf.keras.layers.Dense(1)])
        expected = model(x_neural, training=False).numpy().reshape(-1).tolist()
        model.save(directory / (name + '.keras'))  # No neural fitting needed.
        np.savez(directory/(name+'.npz'),X=x_neural,expected=np.array(expected),raw_expected=scaler.inverse_transform(np.array(expected).reshape(-1,1)))
        result['models'][name] = {'family': 'neural', 'file': name + '.keras', 'expected': expected, 'compatibility':name+'.npz'}
        tf.keras.backend.clear_session()
    for path in directory.glob('*.npz'):
        with np.load(path,allow_pickle=False) as arrays:
            for key in arrays.files:
                assert arrays[key].dtype.kind!='O', 'Compatibility fixtures must not need pickle'
    result['files'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}
    (directory / 'expected.json').write_text(json.dumps(result), encoding='utf-8')
    print(json.dumps({'synthetic_artifacts_created': True, 'model_count': len(result['models']), 'provider_called': False}))


def installed(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def check(directory):
    from deployment.rke2.runtime.role_runtime import verify, build_app
    document = verify()
    role = document['role']
    subprocess.run([sys.executable,'-m','pip','check'],check=True)
    assert not installed('nvidia-nccl-cu12')
    assert not installed('shap') and not installed('numba')
    if role in {'tree', 'sync'}:
        assert not installed('tensorflow-cpu') and not installed('tensorflow') and not installed('keras')
    if role in {'neural', 'sync'}:
        assert not installed('xgboost-cpu') and not installed('xgboost') and not installed('lightgbm')
    if role == 'sync':
        assert not installed('numpy') and not installed('pandas') and not installed('fastapi')
        from backend.app.services import region_cleanup_uploader
        assert callable(region_cleanup_uploader.upload_cycle)
        print(json.dumps({'role': role, 'source_verified': True, 'ml_dependencies_absent': True}))
        return
    import joblib
    import numpy as np
    import pandas as pd
    expected = json.loads((directory / 'expected.json').read_text(encoding='utf-8'))
    for name, wanted in expected['files'].items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == wanted
    tested = []
    for name, entry in expected['models'].items():
        if role == 'tree' and entry['family'] != 'tree' or role == 'neural' and entry['family'] != 'neural':
            continue
        if entry['family'] == 'tree':
            model = joblib.load(directory / entry['file'])
            frame = pd.DataFrame(np.load(directory / 'tree-input.npy', allow_pickle=False), columns=['a', 'b', 'c'])
            actual = model.predict(frame)
        else:
            import tensorflow as tf
            tf.config.set_visible_devices([], 'GPU')
            model = tf.keras.models.load_model(directory / entry['file'], compile=False, safe_mode=True)
            actual = model(np.load(directory / 'neural-input.npy', allow_pickle=False), training=False).numpy().reshape(-1)
            tf.keras.backend.clear_session()
        np.testing.assert_allclose(actual, entry['expected'], rtol=1e-5, atol=1e-5)
        tested.append(name)
    preprocessor = joblib.load(directory / 'preprocessor.joblib')
    assert preprocessor['target_scaler'].inverse_transform([[0.0]]).shape == (1, 1)
    if role in {'tree','neural'}:
        subprocess.run(['/opt/kaggle-sdk/bin/python','-m','pip','check'],check=True)
        entries=[{'model_file':str(directory/expected['models'][name]['file']),
                  'compatibility_file':str(directory/expected['models'][name]['compatibility']),
                  'model_family':'legacy_sklearn' if expected['models'][name]['family']=='tree' else 'neural_sequence',
                  'preprocessor_file':str(directory/'preprocessor.joblib')} for name in tested]
        request=Path('/tmp/offline-serving-check.json'); request.write_text(json.dumps(entries))
        subprocess.run([sys.executable,'-B','/app/kaggle-project/scripts/cloud_training/verify_execution.py','--request',str(request)],
                       check=True,cwd='/app/kaggle-project',timeout=120)
    # Construct routes without starting lifespan/watchers, DB or provider work.
    if role in {'tree', 'neural'}:
        os.environ['TRAFFIC_TRAINING_ROLE'] = 'tree' if role == 'tree' else 'recurrent'
        os.environ['POD_NODE_NAME'] = 'mlops-node-02' if role == 'tree' else 'mlops-node-03'
        os.environ['POD_NAME'] = 'train-tree-test' if role == 'tree' else 'train-neural-test'
    else:
        os.environ['POD_NAME'] = 'api-test'
    os.environ['REGION_CLEANUP_ROLE'] = 'api' if role == 'api' else os.environ['TRAFFIC_TRAINING_ROLE']
    app = build_app()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    try:
        schema = app.openapi()['paths']
        run = '00000000-0000-0000-0000-000000000001'
        if role == 'api':
            assert not any(p.startswith('/api/v1/internal/training-runs') for p in schema)
            assert client.post('/api/v1/internal/training-runs/' + run + '/execute/tree').status_code == 404
            assert client.get('/api/v1/health/serving-ready').status_code == 503
            health = client.get('/api/v1/health/release').json()
        else:
            assert '/api/v1/internal/training-runs/{run_id}/control/{branch}/start' in schema
            assert not any(p.startswith('/api/v1/admin/') for p in schema)
            assert client.post('/api/v1/internal/training-runs/' + run + '/control/tree/start').status_code == 403
            valid = {'X-Internal-Token': os.environ['INTERNAL_TRAINING_TOKEN']}
            assert client.post('/api/v1/internal/training-runs/' + run + '/execute/tree', headers=valid).status_code == 409
            wrong = 'lstm' if role == 'tree' else 'tree'
            assert client.post('/api/v1/internal/training-runs/' + run + '/control/' + wrong + '/start', headers=valid).status_code == 409
            health = client.get('/health/release').json()
        assert health['commit'] == document['commit'] and health['release_id'] == document['source_id']
        forbidden = ['src.pipeline', 'src.time_series_training', 'src.models', 'src.lstm_sequences', 'src.shap_reporting']
        assert not any(name in sys.modules for name in forbidden)
    finally:
        client.close()
    print(json.dumps({'role': role, 'verified_models': tested, 'role_boundary_verified': True,
                      'lifespan_started': False, 'provider_called': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['fixtures', 'check'])
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    (fixtures if args.action == 'fixtures' else check)(args.directory)
