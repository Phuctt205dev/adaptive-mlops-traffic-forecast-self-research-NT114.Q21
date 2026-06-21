# Adaptive MLOps Traffic Forecast

> Phase 0 architecture and application-management contracts are documented in
> [`docs/phase-0/README.md`](docs/phase-0/README.md). The approved chronological
> split policy for the upcoming multi-region system is 70% Development, 15%
> Final Test, and 15% Production. The current implementation still uses fixed
> date boundaries until the pipeline refactor phase.
>
> Phase 1 local infrastructure instructions are documented in
> [`docs/phase-1/README.md`](docs/phase-1/README.md).
>
> Phase 2 backend and database instructions are documented in
> [`docs/phase-2/README.md`](docs/phase-2/README.md).
>
> Deployment Phase 1 configuration for GitHub Pages + EKS is documented in
> [`docs/deploy/phase-1-config.md`](docs/deploy/phase-1-config.md).
>
> Deployment Phase 2 frontend GitHub Pages instructions are documented in
> [`docs/deploy/phase-2-github-pages.md`](docs/deploy/phase-2-github-pages.md).

Dự án dự báo lưu lượng giao thông theo giờ. Hệ thống huấn luyện 8 biến thể
model, so sánh bằng time-series cross-validation và lưu model tốt nhất thành
Champion.

## 1. Luồng hoạt động

```text
Dữ liệu CSV gốc
    |
    v
Chuẩn hóa thành chuỗi giờ liên tục
    |
    v
Tạo lag và rolling feature
    |
    v
Train 8 biến thể model
    |
    v
So sánh CV Mean MAE
    |
    v
Lưu model thắng vào models/champion/
```

Quy tắc chia dữ liệu:

```text
Development : từ đầu dữ liệu đến 2015-09-30 23:00
Final Test  : từ 2015-10-01 đến 2015-12-31 23:00
Production  : từ 2016-01-01, không dùng để train hoặc chọn model
CV          : 5 expanding-window folds, không shuffle
```

## 2. Cấu trúc dễ nhớ

```text
.
|-- app.py                         FastAPI và giao diện dự đoán
|-- retrain_job.py                 Theo dõi drift và retrain production cũ
|-- scripts/
|   |-- data/
|   |   |-- prepare_hourly_data.py
|   |   `-- create_time_series_features.py
|   `-- training/
|       |-- train_all_models.py
|       |-- train_random_forest_no_lag.py
|       |-- train_random_forest_lag.py
|       |-- train_xgboost_no_lag.py
|       |-- train_xgboost_lag.py
|       |-- train_lightgbm_no_lag.py
|       |-- train_lightgbm_lag.py
|       |-- train_lstm.py
|       `-- train_gru.py
|-- src/                            Code xử lý chính được các script sử dụng
|-- tests/                          Unit test
|-- docs/                           Hướng dẫn và tài liệu cũ
|-- data/                           Dữ liệu
|-- models/                         Model đã huấn luyện
`-- results/                        Báo cáo và bảng xếp hạng
```

Quy ước:

- `scripts/data/`: các lệnh tạo dữ liệu đầu vào cho model.
- `scripts/training/`: các lệnh train và chọn model.
- `src/`: phần logic dùng lại, thường không chạy trực tiếp.
- `app.py` và `retrain_job.py`: giữ ở thư mục gốc vì Docker gọi trực tiếp.

## 3. Cài đặt

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip check
```

Mọi lệnh dưới đây cần chạy tại thư mục gốc của dự án.

## 4. Chuẩn bị dữ liệu

### Bước 1: tạo chuỗi giờ liên tục

```powershell
python -m scripts.data.prepare_hourly_data
```

Nguồn mặc định:

```text
data/raw/TrafficVolumeData_original_2012_2017.csv
```

Kết quả chính:

```text
data/processed/TrafficVolumeData_hourly.csv
data/processed/TrafficVolumeData_hourly_audit.csv
data/processed/hourly_quality_report.json
```

### Bước 2: tạo lag và rolling feature

```powershell
python -m scripts.data.create_time_series_features
```

Kết quả:

```text
data/processed/TrafficVolumeData_features.csv
data/processed/time_series_feature_report.json
```

## 5. Huấn luyện model

### Train toàn bộ và chọn Champion

```powershell
python -m scripts.training.train_all_models --max-epochs 20
```

Lệnh này lần lượt train:

1. Random Forest không lag
2. Random Forest có lag
3. XGBoost không lag
4. XGBoost có lag
5. LightGBM không lag
6. LightGBM có lag
7. LSTM với sequence 168 giờ
8. GRU với sequence 168 giờ

Model có `CV Mean MAE` thấp nhất được chọn. `Final Test MAE` chỉ dùng để báo
cáo lần cuối, không dùng để thay đổi thứ hạng.

### Train riêng một model

```powershell
python -m scripts.training.train_xgboost_lag
python -m scripts.training.train_lightgbm_no_lag
python -m scripts.training.train_lstm --max-epochs 20
python -m scripts.training.train_gru --max-epochs 20
```

### Kiểm tra nhanh

```powershell
python -m scripts.training.train_all_models `
  --only lightgbm_no_lag lightgbm_lag `
  --cv-splits 2

python -m scripts.training.train_lstm `
  --cv-splits 2 `
  --max-epochs 1 `
  --quiet
```

Kết quả smoke test không dùng để kết luận model nào tốt nhất.

### Xếp hạng lại từ report có sẵn

```powershell
python -m scripts.training.train_all_models --skip-training
```

Chỉ dùng khi đã có đủ 8 report được tạo với cùng cấu hình và cùng cách chia
dữ liệu.

## 6. Kết quả huấn luyện

```text
results/time_series_cross_validation/  Report của từng model
results/model_selection/               Bảng xếp hạng 8 model
models/time_series/cross_validation/   Artifact của từng model
models/champion/                       Model Champion có version
```

Hai file nên xem trước:

```text
results/model_selection/eight_model_ranking.csv
models/champion/best_model_info.json
```

## 7. Chạy API và Docker

Chạy trực tiếp:

```powershell
uvicorn app:app --host 0.0.0.0 --port 8000
```

Chạy bằng Docker:

```powershell
docker compose up --build -d
docker compose ps
docker compose logs -f
```

Địa chỉ:

```text
http://localhost:8000/
http://localhost:8000/health
http://localhost:8000/docs
```

Lưu ý: API production cũ vẫn đọc `models/best_model.pkl`. Champion mới trong
`models/champion/` chưa được nối vào API và drift worker.

## 8. Chạy test

```powershell
python -m unittest discover -s tests -v
```

## 9. Tài liệu nên đọc

```text
docs/guides/HUONG_DAN_TRAIN_8_MODELS.md
docs/phases/GIAI_DOAN_1_CHUAN_HOA_TIME_SERIES.md
docs/phases/GIAI_DOAN_2_FEATURE_TIME_SERIES.md
docs/changes/CHANGES_CHAMPION_CHALLENGER.md
```
