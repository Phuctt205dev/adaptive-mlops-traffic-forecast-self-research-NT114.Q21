# Tài liệu lưu trữ: Giai đoạn 4B phiên bản cũ

## Mục tiêu

Dùng tensor 168 giờ đã tạo ở bước trước để huấn luyện hai mạng neural:

```text
LSTM
GRU
```

Cả hai nhận cùng một đầu vào:

```text
(số mẫu, 168 giờ, 30 feature mỗi giờ)
```

và dự đoán `traffic_volume` của giờ tiếp theo.

## Kiến trúc

Mỗi model có:

```text
LSTM hoặc GRU: 48 units
Dense: 24 units, activation ReLU
Dropout: 0.2
Dense đầu ra: 1 giá trị traffic đã scale
```

Code xây model nằm trong:

```text
src/neural_time_series_training.py
```

`Huber loss` được dùng vì nó ít bị một vài giờ traffic bất thường làm lệch
quá mạnh như lỗi bình phương.

## Cách chống train quá lâu

`EarlyStopping` dừng khi validation loss không tốt lên trong 4 epoch và khôi
phục bộ trọng số tốt nhất.

`ReduceLROnPlateau` giảm learning rate khi model học chậm lại.

Test không được dùng để chọn giữa LSTM và GRU. Model có validation MAE thấp hơn
sẽ là neural model thắng.

## Artifact

Sau khi chạy, chương trình tạo:

```text
models/time_series/neural/lstm.keras
models/time_series/neural/gru.keras
models/time_series/neural/lstm_history.json
models/time_series/neural/gru_history.json
models/time_series/neural/training_report.json
results/lstm_test_predictions.csv
results/gru_test_predictions.csv
```

## Cách chạy

```powershell
.venv\Scripts\python.exe train_lstm_gru.py
```

Có thể giảm số epoch để thử nhanh:

```powershell
.venv\Scripts\python.exe train_lstm_gru.py --max-epochs 2
```

## Chạy test

```powershell
.venv\Scripts\python.exe -m unittest tests.test_neural_time_series_training -v
```

Bước này mới chọn model tốt hơn giữa LSTM và GRU. Việc so sánh neural model
thắng với LightGBM/XGBoost là bước tiếp theo.

## Kết quả chạy ngày 10/06/2026

```text
LSTM
Validation MAE : 266.2621
Test MAE       : 275.5245
Thời gian train: 105.5715 giây
Kích thước     : 230.672 bytes

GRU
Validation MAE : 237.0237
Test MAE       : 259.6104
Thời gian train: 104.5378 giây
Kích thước     : 186.886 bytes
```

GRU được chọn là neural model tốt hơn vì validation MAE thấp hơn. GRU cũng có
ít tham số hơn:

```text
LSTM: 16.369 tham số
GRU : 12.721 tham số
```

Đây chưa phải model production. Bước sau vẫn phải so sánh GRU với model cây
trên đúng test window, đồng thời xét tốc độ, RAM và kích thước artifact.
