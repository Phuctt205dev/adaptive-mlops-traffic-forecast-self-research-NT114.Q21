# Hướng dẫn train 8 model và chọn best model

## 1. Tám biến thể là gì?

Ba thuật toán cây được thử theo hai cách:

```text
Random Forest không lag
Random Forest có lag
XGBoost không lag
XGBoost có lag
LightGBM không lag
LightGBM có lag
LSTM sequence 168 giờ
GRU sequence 168 giờ
```

`Không lag` nghĩa là model dùng thời tiết và lịch của giờ cần dự đoán.

`Có lag` nghĩa là model nhận thêm traffic quá khứ, ví dụ 1 giờ, 24 giờ và
168 giờ trước cùng các rolling feature.

LSTM/GRU nhận nguyên một chuỗi 168 giờ thay vì một dòng dữ liệu.

## 2. Chính sách dữ liệu chung

Cả 8 model dùng cùng:

```text
Development : đầu dữ liệu -> 2015-09-30 23:00
Final Test  : 2015-10-01 -> 2015-12-31 23:00
Production  : từ 2016-01-01, không dùng để train
CV          : 5 expanding-window folds
Random seed : 42
```

Chỉ target quan sát thật được dùng làm nhãn. Final Test MAE chỉ để báo cáo,
không dùng để đổi thứ hạng.

## 3. Chạy riêng từng model

```powershell
python -m scripts.training.train_random_forest_no_lag
python -m scripts.training.train_random_forest_lag

python -m scripts.training.train_xgboost_no_lag
python -m scripts.training.train_xgboost_lag

python -m scripts.training.train_lightgbm_no_lag
python -m scripts.training.train_lightgbm_lag

python -m scripts.training.train_lstm --max-epochs 20
python -m scripts.training.train_gru --max-epochs 20
```

Mỗi file sẽ in:

```text
MAE từng fold
CV Mean MAE
CV MAE Standard Deviation
Final Test MAE
Đường dẫn report
Đường dẫn model
```

## 4. Chạy pipeline đầy đủ

```powershell
python -m scripts.training.train_all_models --max-epochs 20
```

Pipeline làm tuần tự:

```text
Gọi 8 file train
-> đọc 8 report JSON
-> kiểm tra cùng fold và cùng khoảng thời gian
-> xếp hạng bằng CV Mean MAE
-> lưu model thắng thành Champion có version
```

LSTM/GRU chạy lâu vì mỗi model train 5 fold và thêm một lần train cuối.

## 5. Quy tắc chọn best model

Thứ tự so sánh:

```text
1. CV Mean MAE thấp hơn
2. Nếu bằng nhau, CV MAE Std thấp hơn
3. Nếu vẫn bằng nhau, artifact nhỏ hơn
```

Final Test MAE không nằm trong quy tắc chọn.

## 6. Kiểm tra nhanh

Chạy hai model cây với 2 fold:

```powershell
python -m scripts.training.train_all_models `
  --only lightgbm_no_lag lightgbm_lag `
  --cv-splits 2
```

Chạy nhanh LSTM:

```powershell
python -m scripts.training.train_lstm --cv-splits 2 --max-epochs 1 --quiet
```

Các lệnh trên chỉ kiểm tra code. Không dùng kết quả 2 fold hoặc 1 epoch để
so sánh chính thức.

Khi dùng `--only`, pipeline chỉ tạo bảng xếp hạng thử nghiệm và không lưu
Champion. Champion chỉ được lưu khi đủ cả 8 biến thể.

## 7. File kết quả

Report từng model:

```text
results/time_series_cross_validation/<variant>_report.json
results/time_series_cross_validation/<variant>_folds.csv
```

Model từng biến thể:

```text
models/time_series/cross_validation/
```

Bảng xếp hạng:

```text
results/model_selection/eight_model_ranking.csv
results/model_selection/best_model_selection.json
```

Champion:

```text
models/champion/best_model_info.json
models/champion/versions/<version>/
```

Model cây có một file `.pkl`. LSTM/GRU có model `.keras` và file
`preprocessors.pkl`.

## 8. Xem bảng xếp hạng mà không train lại

Chỉ dùng khi 8 report hiện có được tạo bằng cùng cấu hình:

```powershell
python -m scripts.training.train_all_models --skip-training
```

Nếu report khác số fold hoặc khác cửa sổ thời gian, pipeline sẽ dừng thay vì
chọn sai model.

## 9. Chạy unit test

```powershell
python -m unittest tests.test_best_model_selection -v
python -m unittest discover -s tests -v
```

Lưu ý: Champion mới nằm trong `models/champion/`. API production cũ vẫn đang
đọc `models/best_model.pkl`. Cần thêm bộ inference chung tree/no-lag/neural
trước khi nối Champion mới vào FastAPI và drift worker.
