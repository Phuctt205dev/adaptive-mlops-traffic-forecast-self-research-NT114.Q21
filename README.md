# Adaptive MLOps Traffic Forecast

This project implements an adaptive MLOps workflow for traffic volume forecasting. It supports multi-model training, best model selection, data/model versioning, MAE-based drift detection, automatic retraining, and a FastAPI service for online prediction.

## Main Components

- `data/TrafficVolumeData.csv`: source dataset used for training and evaluation.
- `src/preprocess.py`: data loading, preprocessing, time feature engineering, weather one-hot encoding, and time-based splitting.
- `src/train.py`: model training functions for RandomForest, XGBoost, and LightGBM.
- `src/pipeline.py`: training/retraining pipeline, model evaluation, best model selection, MLflow logging, and versioning.
- `src/drift.py`: drift detection based on MAE.
- `src/inference.py`: single-input preprocessing and prediction logic.
- `main.py`: batch orchestrator for model initialization, prediction, drift detection, and retraining.
- `predict.py`: batch prediction script that saves predictions and metrics to `results/`.
- `app.py`: FastAPI service for online traffic prediction.
- `docs/index.html`: simple web interface that calls the prediction API.
- `models/`: stores `best_model.pkl` and model versions.
- `data_versions/`: stores versioned training datasets.
- `monitoring/`: stores drift history.
- `mlruns/` and `mlflow.db`: MLflow experiment tracking artifacts.

## Workflow

### 1. Training Pipeline

```text
TrafficVolumeData.csv
  -> preprocess()
  -> filter training window
  -> create data version
  -> time-based train/validation/test split
  -> train RandomForest, XGBoost, and LightGBM
  -> evaluate on validation set
  -> select the best model by RMSE
  -> evaluate the best model on test set
  -> save best_model.pkl and model_vN.pkl
  -> log metrics and artifacts to MLflow
```

### 2. Drift Detection and Retraining

`main.py` checks whether `models/best_model.pkl` exists.

- If no model exists, it runs the initial training pipeline.
- If a model exists, it loads the data, predicts on a new data window, and computes MAE.
- If `MAE > MAE_THRESHOLD`, drift is logged and the model is retrained.
- If no drift is detected, the current model is kept.

### 3. Online Prediction API

`app.py` exposes the following endpoints:

- `GET /health`: service health check.
- `GET /model-info`: returns the model file currently used by the API.
- `POST /predict`: receives `date_time`, fetches weather data from Open-Meteo, builds features, and returns a traffic prediction.

Example input:

```json
{
  "date_time": "2013-12-01T08:00"
}
```

Example output:

```json
{
  "prediction": 4321,
  "features_used": {
    "date_time": "2013-12-01T08:00",
    "temperature": 20,
    "humidity": 60
  }
}
```

## Installation

Python 3.11 or a compatible version is recommended.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

If the `.venv` folder already exists, activate it directly:

```powershell
.venv\Scripts\activate
```

## Usage

### Run the Training/Drift/Retraining Orchestrator

```powershell
python main.py
```

On the first run, if `models/best_model.pkl` does not exist, the script trains the initial model and asks you to run it again. The main configuration values are currently hardcoded in `main.py`:

- `TRAIN_START_DATE`
- `TRAIN_END_DATE`
- `PREDICT_START`
- `PREDICT_END`
- `MAE_THRESHOLD`
- `MODEL_PATH`

### Run Batch Prediction

```powershell
python predict.py
```

Outputs are saved to:

- `results/predict.csv`
- `results/predict_log.csv`

### Test Single-Input Inference

```powershell
python test_inference.py
```

### Run the API Locally

```powershell
uvicorn app:app --host 0.0.0.0 --port 8000
```

After the API starts, open:

- `http://localhost:8000/health`
- `http://localhost:8000/model-info`
- `http://localhost:8000/docs`

### Run with Docker

```powershell
docker compose up --build
```

The service exposes port `8000`.

## Monitoring and Versioning

The project stores several artifacts for tracking the MLOps workflow:

- `models/model_versions.csv`: list of model versions.
- `models/model_vN.pkl`: versioned trained models.
- `models/best_model.pkl`: model currently used for inference.
- `data_versions/version_log.csv`: metadata for data versions.
- `data_versions/data_vN.csv`: training data snapshots.
- `monitoring/drift_history.csv`: MAE, threshold, and drift decision history.
- `results/predict_log.csv`: metrics from batch prediction runs.
- `mlflow.db` and `mlruns/`: MLflow tracking data.

## Operational Notes

- `MAE_THRESHOLD` in `main.py` should be tuned based on historical model performance. If it is too low, the system will retrain too frequently.
- The API in `app.py` calls Open-Meteo, so online prediction requires internet access.
- `predict.py` currently uses `plt.show()`, which may not be suitable for server or headless environments.
- Scripts in `tools/` are experimental model testing scripts, not the main production workflow.
- Some Vietnamese comments in the source files appear to have encoding issues, but the program logic is still readable.

## Simplified Folder Structure

```text
.
|-- app.py
|-- main.py
|-- predict.py
|-- test_inference.py
|-- requirements.txt
|-- Dockerfile
|-- docker-compose.yml
|-- data/
|-- data_versions/
|-- docs/
|-- mlruns/
|-- models/
|-- monitoring/
|-- results/
|-- src/
|   |-- preprocess.py
|   |-- train.py
|   |-- pipeline.py
|   |-- inference.py
|   `-- drift.py
`-- tools/
```
