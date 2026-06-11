# Tài liệu lưu trữ: Giai đoạn 4 sequence phiên bản cũ

## 1. Mục tiêu

LightGBM và XGBoost nhận một bảng hai chiều:

```text
(samples, features)
```

LSTM cần tensor ba chiều:

```text
(samples, time_steps, features)
```

Trong dự án này:

```text
time_steps = 168 giờ = 7 ngày
```

Mỗi sample dùng 168 giờ đã xảy ra để dự đoán traffic của giờ tiếp theo.

## 2. Lag 168 giờ khác sequence 168 giờ

Lag chỉ là một con số:

```text
traffic_volume_lag_168h = traffic đúng một tuần trước
```

Sequence chứa toàn bộ 168 dòng:

```text
t-168: traffic, thời tiết, giờ, thứ...
t-167: traffic, thời tiết, giờ, thứ...
...
t-1  : traffic, thời tiết, giờ, thứ...
→ dự đoán target tại t
```

## 3. Các file được thêm

```text
src/lstm_sequences.py
prepare_lstm_sequences.py
tests/test_lstm_sequences.py
```

Artifact sinh tự động:

```text
data/processed/lstm_sequences_168h.npz
data/processed/lstm_sequence_report.json
models/time_series/lstm_sequence_preprocessors.pkl
```

## 4. Feature trong mỗi giờ

Mỗi giờ có 30 feature:

- 10 feature thời tiết dạng số.
- `traffic_history_value`.
- Cờ traffic có phải quan sát thật.
- Cờ ngày lễ và cuối tuần.
- Sin/cos của giờ, thứ và tháng.
- One-hot của `weather_type`.

`weather_description` không được đưa vào sequence vì có nhiều nhãn chi tiết,
làm tăng số chiều nhưng chưa chứng minh có lợi cho LSTM.

## 5. Traffic causal

Code không dùng trực tiếp target suy luận offline làm lịch sử.

```python
source["traffic_history_value"] = build_causal_target_series(
    source
)
```

Khi target thật bị thiếu, chỉ dùng thông tin quá khứ:

```text
target thật 168 giờ trước
target thật 24 giờ trước
median tối đa 168 giờ quá khứ
giá trị causal gần nhất
```

## 6. Cách tạo một sequence

Với target tại vị trí `t`:

```python
start_index = target_index - sequence_length
sequence = timeline[start_index:target_index]
```

Sequence kết thúc tại `t-1`. Target tại `t` không xuất hiện trong input của
chính nó.

## 7. Chỉ target thật được dùng làm nhãn

```python
candidate_indices = np.arange(sequence_length, len(source_df))
eligible = candidate_indices[
    target_observed[candidate_indices]
]
```

Giờ có target suy luận vẫn có thể nằm trong phần lịch sử với cờ chất lượng,
nhưng không được dùng làm `y`.

## 8. Scaler chỉ fit bằng train

Ba bộ preprocessing được lưu:

```text
feature_scaler
weather_encoder
target_scaler
```

Chúng chỉ học dữ liệu train:

```text
Feature scaler fit đến: 2014-01-17 23:00:00
Validation bắt đầu     : 2014-01-18 01:00:00
```

Validation và test không tham gia tính mean, standard deviation hoặc danh sách
category.

## 9. Kết quả dữ liệu thật

```text
Nguồn hourly       : 16.193 dòng
Sequence length    : 168 giờ
Feature mỗi giờ    : 30

Train X            : (9.618, 168, 30)
Validation X       : (2.061, 168, 30)
Test X             : (2.061, 168, 30)

Train y            : (9.618, 1)
Validation y       : (2.061, 1)
Test y             : (2.061, 1)
```

Test window:

```text
2014-04-14 16:00:00
đến
2014-08-08 01:00:00
```

Khoảng này khớp test window của LightGBM/XGBoost, giúp so sánh công bằng ở
bước train LSTM/GRU.

## 10. Định dạng file NPZ

File chứa:

```text
X_train, y_train, raw_y_train, timestamps_train
X_validation, y_validation, raw_y_validation, timestamps_validation
X_test, y_test, raw_y_test, timestamps_test
```

`y_*` đã scale để neural network học ổn định.

`raw_y_*` giữ traffic thật để tính MAE bằng đơn vị xe.

## 11. Cách chạy

```powershell
.venv\Scripts\python.exe prepare_lstm_sequences.py
```

Tùy chỉnh chiều dài:

```powershell
.venv\Scripts\python.exe prepare_lstm_sequences.py `
  --sequence-length 168
```

## 12. Chạy test

```powershell
.venv\Scripts\python.exe -m unittest tests.test_lstm_sequences -v
```

Test kiểm tra:

1. Tensor có đúng ba chiều.
2. Sequence kết thúc tại giờ trước target.
3. Target suy luận không được dùng làm nhãn.
4. Thay target hiện tại không làm input của chính nó thay đổi.
5. Feature scaler chỉ fit bằng train.
6. Target scaler có thể inverse transform.
7. Artifact được lưu và đọc lại.

## 13. Trạng thái

Phần tạo sequence đã hoàn thành.

Chưa thực hiện trong bước này:

- Xây kiến trúc LSTM.
- Xây kiến trúc GRU.
- Train neural model.
- So sánh neural model với XGBoost.

Đó là bước tiếp theo.
