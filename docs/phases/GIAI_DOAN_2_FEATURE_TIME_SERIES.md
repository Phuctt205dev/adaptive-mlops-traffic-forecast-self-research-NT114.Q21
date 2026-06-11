# Giai đoạn 2: Tạo feature cho bài toán time series

## 1. Giai đoạn 2 làm gì?

Giai đoạn 1 đã biến dữ liệu thành một dòng cho mỗi giờ:

```text
09:00
10:00
11:00
12:00
```

Giai đoạn 2 giúp model nhìn được lịch sử. Ví dụ, khi cần dự đoán traffic lúc
`09:00`, model có thể tham khảo:

```text
Traffic 1 giờ trước
Traffic 24 giờ trước
Traffic 168 giờ trước, tức cùng giờ của một tuần trước
Trung bình traffic của 24 giờ vừa qua
Hôm nay là thứ mấy, cuối tuần hay ngày lễ
```

Giai đoạn này chưa train LightGBM hoặc LSTM. Nó chỉ chuẩn bị bảng feature.

## 2. Các file mới

```text
src/time_series_features.py
scripts/data/create_time_series_features.py
tests/test_time_series_features.py
```

File được sinh tự động:

```text
data/processed/TrafficVolumeData_features.csv
data/processed/time_series_feature_report.json
```

## 3. Luồng hoạt động

```text
TrafficVolumeData_hourly.csv
              +
TrafficVolumeData_hourly_audit.csv
              |
              v
scripts/data/create_time_series_features.py
              |
              v
src/time_series_features.py
              |
              v
TrafficVolumeData_features.csv
```

CSV audit cần thiết vì nó cho biết `traffic_volume` nào là số quan sát thật.

## 4. Kiểm tra dữ liệu đầu vào

Hàm:

```python
def merge_hourly_with_audit(hourly_df, audit_df):
```

Hàm kiểm tra:

- Có đủ các cột bắt buộc.
- `date_time` không bị trùng.
- Hai file có cùng tập `date_time`.
- Hai dòng liên tiếp cách nhau đúng một giờ.

Nếu chuỗi chưa liên tục, code yêu cầu chạy lại:

```powershell
python -m scripts.data.prepare_hourly_data
```

## 5. Feature lịch

Hàm:

```python
def add_calendar_features(dataframe):
```

Các feature cơ bản:

```text
hour          = giờ trong ngày, từ 0 đến 23
day_of_week   = thứ trong tuần, từ 0 đến 6
day_of_month  = ngày trong tháng
month         = tháng
day_of_year   = ngày thứ bao nhiêu trong năm
is_weekend    = có phải cuối tuần không
```

Ngày lễ dạng chữ vẫn được giữ ở `is_holiday`. Code tạo thêm bản dạng số:

```python
result["is_holiday_binary"] = (
    result["is_holiday"].notna().astype(int)
)
```

## 6. Tại sao cần sin và cos?

Nếu chỉ dùng số giờ:

```text
23 giờ và 0 giờ cách nhau 23 đơn vị
```

Nhưng ngoài đời chúng chỉ cách nhau một giờ. Sin và cos đặt các giờ lên một
vòng tròn:

```python
result["hour_sin"] = np.sin(2 * np.pi * result["hour"] / 24)
result["hour_cos"] = np.cos(2 * np.pi * result["hour"] / 24)
```

Code cũng làm tương tự cho thứ trong tuần và tháng trong năm.

## 7. Lag feature là gì?

Lag có thể hiểu là “nhìn lại quá khứ”.

```text
traffic_volume_lag_1h   = traffic của 1 giờ trước
traffic_volume_lag_24h  = traffic của 24 giờ trước
traffic_volume_lag_168h = traffic của một tuần trước
```

Các lag mặc định:

```python
DEFAULT_LAG_HOURS = (1, 2, 3, 6, 12, 24, 48, 168)
```

Ví dụ:

```text
08:00 traffic = 4000
09:00 traffic = 5000
```

Tại dòng `09:00`:

```text
traffic_volume_lag_1h = 4000
traffic_volume        = 5000
```

## 8. Rolling feature là gì?

Rolling là thống kê trên một cửa sổ các giờ gần nhất.

Ví dụ traffic ba giờ trước:

```text
1000, 2000, 3000
```

Các giá trị rolling:

```text
mean   = 2000
median = 2000
min    = 1000
max    = 3000
std    = mức dao động
```

Các cửa sổ mặc định:

```python
DEFAULT_ROLLING_WINDOWS = (3, 6, 12, 24, 168)
```

## 9. Chống nhìn trước đáp án

Đây là quy tắc quan trọng nhất của Giai đoạn 2.

Khi dự đoán traffic lúc `09:00`, model không được dùng traffic thật của chính
`09:00`. Vì vậy rolling luôn bắt đầu bằng:

```python
past_target = causal_target.shift(1)
```

`shift(1)` nghĩa là lùi target xuống một dòng. Dòng hiện tại chỉ nhìn thấy các
giờ trước nó.

Nếu không có bước này, kết quả test có thể rất đẹp nhưng model đã nhìn trộm đáp
án và sẽ hoạt động kém khi triển khai thật.

## 10. Không dùng target suy luận nhìn tương lai

Target suy luận ở Giai đoạn 1 phục vụ việc tạo chuỗi liên tục và kiểm tra dữ
liệu. Một số phép suy luận offline có thể tham khảo cả thời điểm ở phía sau.

Giai đoạn 2 không dùng trực tiếp các target đó làm lịch sử. Code bắt đầu bằng:

```python
observed_target = result["traffic_volume"].where(
    result["target_observed"]
)
```

Nếu một giờ không có target thật, code chỉ tìm thông tin trong quá khứ:

```text
1. Target quan sát thật ở 168 giờ trước.
2. Target quan sát thật ở 24 giờ trước.
3. Median của tối đa 168 giờ quá khứ.
4. Giá trị quá khứ gần nhất nếu vẫn chưa đủ dữ liệu.
```

Các cờ như:

```text
lag_24h_target_observed
history_observed_ratio_24h
```

giúp model biết lịch sử đó có bao nhiêu phần là dữ liệu thật.

## 11. Target nào được giữ để train?

Chỉ target quan sát thật được giữ:

```python
training_mask = (
    featured["target_observed"]
    & featured[history_columns].notna().all(axis=1)
)
```

Target suy luận có ích để phân tích nhưng không được giả làm đáp án huấn luyện.

## 12. Tại sao mất 168 giờ đầu?

Feature lớn nhất là:

```text
traffic_volume_lag_168h
rolling 168 giờ
```

Tại giờ đầu tiên, chưa có lịch sử một tuần. Code phải chờ đủ 168 giờ trước khi
tạo một dòng hoàn chỉnh.

Đây gọi là giai đoạn `warm-up`, không phải dữ liệu bị xóa nhầm.

## 13. Kết quả trên dữ liệu hiện tại

```text
Dòng hourly đầu vào             : 16.193
Target suy luận không dùng train: 2.295
Dòng warm-up/incomplete bị bỏ   : 158
Dòng có thể dùng huấn luyện     : 13.740
Số cột đầu ra                   : 74
```

Khoảng thời gian đầu ra:

```text
2012-10-09 09:00:00
đến
2014-08-08 01:00:00
```

Cột `traffic_volume` luôn nằm cuối file để dễ tách:

```python
X = dataframe.drop(columns=["date_time", "traffic_volume"])
y = dataframe["traffic_volume"]
```

`is_holiday` vẫn có ô trống ở ngày thường. Đây là dữ liệu hợp lệ, không phải
feature số bị thiếu. Khi train, cột chữ cần được encode và feature số cần được
scale tùy loại model.

## 14. Cách chạy

Chạy Giai đoạn 1 trước:

```powershell
.venv\Scripts\python.exe -m scripts.data.prepare_hourly_data
```

Sau đó chạy Giai đoạn 2:

```powershell
.venv\Scripts\python.exe -m scripts.data.create_time_series_features
```

Tùy chỉnh lag và rolling:

```powershell
.venv\Scripts\python.exe -m scripts.data.create_time_series_features `
  --lags 1,3,6,24,168 `
  --rolling-windows 6,24,168
```

## 15. Chạy test

Chỉ test Giai đoạn 2:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_time_series_features -v
```

Toàn bộ test dự án:

```powershell
.venv\Scripts\python.exe -m unittest discover -v
```

Test kiểm tra:

1. Lag lấy đúng dữ liệu quá khứ.
2. Rolling không chứa target hiện tại.
3. Thay target hiện tại không làm feature của chính nó thay đổi.
4. Target suy luận không được dùng làm nhãn.
5. Giá trị target suy luận không lọt vào lịch sử.
6. Cờ target quan sát của lag hoạt động đúng.
7. Chuỗi không liên tục bị từ chối.
8. Feature lịch được tạo.
9. CSV và JSON được lưu thành công.

## 16. Giai đoạn này chưa làm gì?

Chưa làm:

- Chia train/validation/test chính thức cho model time series mới.
- Fit scaler chỉ trên tập train.
- Encode categorical bằng bộ encoder được lưu lại.
- Tạo tensor ba chiều cho LSTM.
- Train và so sánh LightGBM time series với LSTM.
- Tích hợp model mới vào Champion-Challenger và API.

Phần train LightGBM/XGBoost hiện đã được triển khai tại:

```text
docs/phases/GIAI_DOAN_3_HUAN_LUYEN_TIME_SERIES.md
```

LSTM/GRU chưa được train vì môi trường hiện tại chưa có TensorFlow/PyTorch.
