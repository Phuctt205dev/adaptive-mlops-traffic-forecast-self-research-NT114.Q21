# Tài liệu lưu trữ: Giai đoạn 3 phiên bản cũ

## 1. Mục tiêu

Giai đoạn 1 tạo dữ liệu liên tục theo giờ.

Giai đoạn 2 tạo feature lịch sử như:

```text
traffic 1 giờ trước
traffic 24 giờ trước
traffic 168 giờ trước
trung bình các giờ trước
giờ trong ngày và thứ trong tuần
```

Giai đoạn 3 dùng bảng feature đó để:

1. Chia train, validation và test theo thời gian.
2. Train LightGBM và XGBoost.
3. So sánh với các cách dự đoán đơn giản.
4. Chọn model bằng validation MAE.
5. Đánh giá model đã chọn trên test.
6. Lưu model, báo cáo, dự đoán và feature importance.

Model mới vẫn là model thử nghiệm. Nó chưa thay thế champion của API.

## 2. Các file mới

```text
src/time_series_training.py
train_time_series_models.py
tests/test_time_series_training.py
```

Artifact được tạo:

```text
models/time_series/lightgbm_candidate.pkl
models/time_series/xgboost_candidate.pkl
models/time_series/best_time_series_model.pkl
models/time_series/training_report.json
results/time_series_test_predictions.csv
results/time_series_feature_importance.csv
```

## 3. Luồng hoạt động

```text
TrafficVolumeData_features.csv
              |
              v
Chia theo thời gian
              |
       +------+------+
       |             |
       v             v
   LightGBM       XGBoost
       |             |
       +------+------+
              |
              v
Chọn validation MAE thấp nhất
              |
              v
Fit lại bằng train + validation
              |
              v
Đánh giá một lần trên test
```

## 4. Vì sao không được chia ngẫu nhiên?

Dữ liệu time series có thứ tự:

```text
quá khứ -> hiện tại -> tương lai
```

Nếu shuffle, một dòng năm 2014 có thể lọt vào train trong khi dòng năm 2013
nằm ở test. Khi đó model đã học tương lai.

Code chia theo thứ tự:

```python
train_df = ordered.iloc[:train_end]
validation_df = ordered.iloc[train_end:validation_end]
test_df = ordered.iloc[validation_end:]
```

Kết quả hiện tại:

```text
Train      : 9.618 dòng, 2012-10-09 đến 2014-01-18
Validation : 2.061 dòng, 2014-01-18 đến 2014-04-14
Test       : 2.061 dòng, 2014-04-14 đến 2014-08-08
```

## 5. Vai trò của ba tập dữ liệu

### Train

Model học quy luật từ tập này.

### Validation

Hai model được so sánh trên tập này. Model có MAE thấp hơn được chọn.

### Test

Test là đề thi cuối. Nó không được dùng để train hoặc chọn model.

Nếu xem test nhiều lần rồi sửa model theo test, test không còn là dữ liệu mới.

## 6. Preprocessing được đóng gói trong model

CSV có cả cột chữ và cột số.

Cột chữ:

```text
is_holiday
weather_type
weather_description
```

Chúng được đổi thành one-hot:

```python
OneHotEncoder(handle_unknown="ignore")
```

Nếu validation gặp loại thời tiết chưa xuất hiện trong train, pipeline vẫn
chạy và không bị lỗi.

Cột số được:

```text
điền median nếu thiếu
chuẩn hóa bằng StandardScaler
```

Toàn bộ được đóng gói:

```python
Pipeline(
    steps=[
        ("preprocessor", preprocessor),
        ("model", estimator),
    ]
)
```

Khi lưu `.pkl`, encoder, scaler và model nằm trong cùng một artifact. Do đó
lúc dự đoán không thể vô tình dùng sai scaler.

## 7. Chống rò rỉ từ preprocessing

Pipeline chỉ fit trên train:

```python
pipeline.fit(X_train, y_train)
```

Validation chỉ gọi:

```python
pipeline.predict(X_validation)
```

Scaler không được nhìn validation hoặc test khi model đang được chọn.

Sau khi chọn thuật toán tốt nhất, một pipeline mới được fit lại bằng:

```text
train + validation
```

Test vẫn được giữ nguyên để đánh giá cuối.

## 8. Baseline là gì?

Baseline là cách đoán đơn giản để kiểm tra model ML có thực sự hữu ích.

Ba baseline:

```text
NaiveLag1Hour    : lấy traffic một giờ trước làm dự đoán
NaiveLag24Hours  : lấy traffic một ngày trước
NaiveLag168Hours : lấy traffic một tuần trước
```

Test MAE:

```text
Lag 1 giờ   : 620,16
Lag 24 giờ  : 603,31
Lag 168 giờ : 399,76
XGBoost     : 178,20
```

Model XGBoost tốt hơn baseline một tuần khoảng:

```text
399,76 - 178,20 = 221,56 xe MAE
```

## 9. Kết quả LightGBM và XGBoost

Validation:

```text
LightGBM MAE : 167,36
XGBoost MAE  : 166,55
```

XGBoost thắng khoảng:

```text
0,81 MAE
```

Khoảng cách nhỏ, nhưng quy tắc từ đầu là chọn model có validation MAE thấp
nhất nên XGBoost được chọn.

LightGBM train nhanh hơn:

```text
LightGBM : khoảng 1,31 giây
XGBoost  : khoảng 5,60 giây
```

Điều này cho thấy không nên kết luận XGBoost luôn tốt hơn. Khi tích hợp
production cần cân nhắc cả độ chính xác, tốc độ và độ ổn định.

## 10. Kết quả test cuối

```text
MAE  : 178,1967 xe
RMSE : 327,8746 xe
MAPE : 8,8307%
WAPE : 5,1882%
R2   : khoảng 0,9742
```

### MAE

Trung bình mỗi dự đoán lệch khoảng 178 xe.

### RMSE

Phạt mạnh các lỗi lớn. RMSE cao hơn MAE cho thấy vẫn có một số thời điểm sai
lệch lớn.

### MAPE

Sai số phần trăm trung bình của từng dòng. Metric này nhạy khi traffic gần 0.

### WAPE

Tổng sai số tuyệt đối chia tổng traffic thật. Với dữ liệu này WAPE khoảng
5,19%.

### R2

Gần 1 nghĩa là model giải thích được phần lớn biến động traffic trong test.

## 11. Feature importance

Các feature đứng đầu:

```text
traffic_volume_lag_168h
traffic_volume_lag_1h
hour_cos
hour
traffic_volume_lag_24h
traffic_volume_rolling_12h_std
```

Ý nghĩa:

- Traffic cùng giờ một tuần trước rất quan trọng.
- Traffic một giờ trước giúp nhận biết tình trạng gần nhất.
- Giờ trong ngày giúp model hiểu giờ cao điểm và giờ vắng.
- Rolling mô tả mức độ dao động gần đây.

Feature importance không khẳng định nguyên nhân. Nó chỉ cho biết model đã sử
dụng feature đó nhiều đến mức nào khi tạo cây.

## 12. Cách chạy

Chạy lần lượt:

```powershell
.venv\Scripts\python.exe prepare_time_series_data.py
.venv\Scripts\python.exe prepare_time_series_features.py
.venv\Scripts\python.exe train_time_series_models.py
```

Chạy riêng test Giai đoạn 3:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_time_series_training -v
```

Chạy toàn bộ test:

```powershell
.venv\Scripts\python.exe -m unittest discover -v
```

## 13. Model được lưu ở đâu?

Model tốt nhất:

```text
models/time_series/best_time_series_model.pkl
```

Đây là một sklearn Pipeline. Cách load:

```python
import joblib

model = joblib.load(
    "models/time_series/best_time_series_model.pkl"
)
prediction = model.predict(feature_dataframe)
```

Artifact hiện tại khoảng 2,97 MB.

## 14. Đây là dự báo loại nào?

Mục tiêu hiện tại:

```text
one-step-ahead forecasting
```

Nghĩa là mỗi lần dự đoán trước một giờ và lịch sử traffic gần nhất đã được cập
nhật.

Ví dụ lúc 09:00, hệ thống đã biết traffic thật đến 08:00 và dự đoán 09:00.

Đây chưa phải dự báo nguyên 24 giờ tương lai trong một lần. Muốn dự báo nhiều
bước, cần:

- Dự đoán đệ quy, hoặc
- Model multi-horizon riêng.

Không được lấy traffic thật của các giờ tương lai để tạo lag khi triển khai.

## 15. Vì sao chưa có LSTM/GRU?

Môi trường hiện tại:

```text
TensorFlow : chưa cài
PyTorch    : chưa cài
```

LSTM/GRU cần một trong các framework trên. Chúng là dependency nặng và có thể
làm Docker image, thời gian build và RAM EC2 tăng đáng kể.

Pipeline không âm thầm cài framework. Báo cáo ghi:

```json
{
  "neural_models": {
    "status": "skipped"
  }
}
```

Đây là giới hạn minh bạch, không phải LSTM đã được train rồi thua XGBoost.

## 16. Giai đoạn 3 chưa làm gì?

Chưa làm:

- Train LSTM hoặc GRU.
- Tìm hyperparameter tối ưu bằng nhiều thử nghiệm.
- Walk-forward validation nhiều cửa sổ.
- Dự báo nhiều giờ tương lai.
- Đưa model time series vào API.
- Đưa model vào Champion-Challenger.
- Thay thế `models/best_model.pkl`.

Model hiện tại có trạng thái:

```text
experimental_not_connected_to_champion_or_api
```

Việc tích hợp production phải được thực hiện riêng và có cơ chế rollback.

Phần tạo sequence LSTM hiện đã được triển khai tại:

```text
docs/phases/GIAI_DOAN_4_SEQUENCE_LSTM.md
```
