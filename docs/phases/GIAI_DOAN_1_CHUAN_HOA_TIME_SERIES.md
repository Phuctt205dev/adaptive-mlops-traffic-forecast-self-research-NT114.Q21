# Giai đoạn 1: Chuẩn hóa dữ liệu time series theo giờ

## 1. Mục tiêu

Trước khi tạo lag feature, LSTM hoặc mô hình time series, dữ liệu cần có một
nhịp thời gian rõ ràng:

```text
09:00
10:00
11:00
12:00
```

Mỗi giờ nên có đúng một dòng.

CSV gốc chưa đáp ứng điều đó vì:

- Có nhiều dòng cùng `date_time`.
- Có những giờ không xuất hiện.
- Các dòng trùng giờ có thể mô tả nhiều loại thời tiết khác nhau.

Giai đoạn 1 chỉ làm sạch trục thời gian. Chưa tạo lag, chưa train model và chưa
thay đổi pipeline production hiện tại.

## 2. Các file được thêm

```text
src/time_series_preprocess.py
scripts/data/prepare_hourly_data.py
tests/test_time_series_preprocess.py
```

File kết quả được sinh tự động:

```text
data/processed/TrafficVolumeData_hourly.csv
data/processed/TrafficVolumeData_hourly_audit.csv
data/processed/hourly_quality_report.json
```

`data/processed/` được thêm vào `.gitignore` vì có thể tạo lại từ CSV gốc.

`TrafficVolumeData_hourly.csv` chỉ chứa đúng 15 cột và đúng thứ tự như
`TrafficVolumeData.csv`. Các cột kỹ thuật dùng để kiểm tra dữ liệu được chuyển
sang `TrafficVolumeData_hourly_audit.csv`.

## 3. Kết quả khảo sát CSV thật

```text
Số dòng ban đầu                   : 15.971
Số thời điểm duy nhất             : 13.898
Số dòng dư do trùng thời điểm     : 2.073
Số nhóm giờ bị trùng              : 1.429
Số giờ bị thiếu                   : 2.295
Số dòng sau khi tạo chuỗi đầy đủ  : 16.193
```

Điểm đáng chú ý:

```text
Số giờ trùng có traffic_volume mâu thuẫn: 0
```

Điều đó nghĩa là các dòng trùng giờ khác nhau về thời tiết hoặc ô nhiễm, nhưng
đều ghi cùng một lượng xe.

## 4. Tại sao timestamp trùng là vấn đề?

Ví dụ dữ liệu có:

```text
10:00, Clouds, traffic=3000
10:00, Rain,   traffic=3000
11:00, Clear,  traffic=3500
```

Một time series theo giờ thường cần:

```text
10:00, một dòng duy nhất
11:00, một dòng duy nhất
```

Nếu giữ cả hai dòng `10:00`, mô hình có thể tưởng đó là hai bước thời gian khác
nhau dù chúng xảy ra cùng lúc.

## 5. Quy tắc gộp các dòng trùng giờ

Hàm:

```python
def aggregate_duplicate_hours(df):
```

### Feature dạng số

```python
aggregations = {
    column: "mean"
    for column in NUMERIC_FEATURE_COLUMNS
}
```

Ví dụ hai dòng cùng giờ có humidity:

```text
60 và 70
```

Sau khi gộp:

```text
humidity = 65
```

### Ngày lễ

```python
"is_holiday": most_common_value
```

Nếu có nhiều dòng trong cùng giờ, code giữ tên ngày lễ xuất hiện nhiều nhất:

```text
Christmas Day
```

### Feature dạng chữ

```python
"weather_type": most_common_value
```

Hàm chọn giá trị xuất hiện nhiều nhất, còn gọi là mode.

### Target

```python
"traffic_volume": "mean"
```

Trong CSV thật, target của các dòng trùng giờ giống nhau nên lấy trung bình
không làm thay đổi giá trị.

Nếu một file khác có target mâu thuẫn, báo cáo JSON sẽ tăng trường:

```text
duplicate_hours_with_target_conflict
```

## 6. Chèn giờ bị thiếu

Hàm:

```python
def insert_missing_hours(hourly_df):
```

Tạo một index đầy đủ:

```python
complete_index = pd.date_range(
    start=hourly_df.index.min(),
    end=hourly_df.index.max(),
    freq="h",
)
```

Ví dụ dữ liệu gốc:

```text
09:00
10:00
12:00
```

Sau khi reindex:

```text
09:00
10:00
11:00  <- được chèn
12:00
```

## 7. Hai cờ quan trọng

Hai cờ này nằm trong `TrafficVolumeData_hourly_audit.csv`, không nằm trong CSV
chính.

### `is_observed_hour`

Cho biết giờ đó có tồn tại trong CSV gốc hay được chèn.

```text
True  = có trong CSV gốc
False = giờ được chèn
```

### `target_observed`

Cho biết `traffic_volume` là đáp án thật hay không.

```text
True  = có target thật
False = không có target thật
```

Hai cột này giúp Giai đoạn sau không vô tình dùng dữ liệu tổng hợp làm đáp án.

## 8. Target suy luận không phải target thật

Phiên bản đầu tiên để `traffic_volume` trống ở các giờ được chèn. Phiên bản mới
điền một giá trị ước lượng để chuỗi dễ đọc và thuận tiện cho việc tạo lag sau
này.

```python
target_observed = False
target_is_imputed = True
```

Hai cờ này có nghĩa:

```text
traffic_volume có số, nhưng số đó được suy luận chứ không phải cảm biến ghi lại
```

Khi huấn luyện model, quy tắc bắt buộc là:

```python
training_source = hourly_df.merge(
    audit_df,
    on="date_time",
    validate="one_to_one",
)
training_df = training_source[
    training_source["target_observed"]
]
```

Không dùng target suy luận làm nhãn train.

## 9. Suy luận traffic thực tế hơn

Traffic có chu kỳ:

```text
07:00 thứ Hai thường giống 07:00 các thứ Hai khác
07:00 Chủ nhật thường không giống 07:00 thứ Hai
```

Vì vậy code ưu tiên:

```python
cùng giờ + cùng thứ + các tuần lân cận
```

Phạm vi tìm kiếm:

```python
SEASONAL_LOOKAROUND_DAYS = 35
```

Nếu không đủ dữ liệu cùng thứ, code dùng cùng giờ ở các ngày lân cận. Chỉ khi
không có mẫu mùa vụ và khoảng thiếu ngắn, code mới nội suy tuyến tính.

Method được ghi trong:

```text
target_imputation_method
```

Các giá trị có thể gặp:

```text
observed
seasonal_same_hour_weekday
seasonal_same_hour
short_gap_linear
unavailable
```

## 10. Điền feature theo độ dài khoảng trống

CSV thật có:

```text
641 đoạn thiếu 1 giờ
110 đoạn thiếu 2 giờ
đoạn dài nhất thiếu 242 giờ
```

Không thể dùng cùng một quy tắc cho tất cả.

### Khoảng thiếu ngắn, tối đa 6 giờ

Feature số dùng nội suy tuyến tính giữa hai mốc quan sát.

Ví dụ:

```text
09:00 temperature=280
10:00 temperature=?
11:00 temperature=284

10:00 được ước lượng thành 282
```

### Khoảng thiếu dài hơn 6 giờ

Không kéo một đường thẳng từ đầu đến cuối khoảng thiếu. Code dùng feature của:

```text
cùng giờ, ưu tiên cùng thứ, trong các tuần gần đó
```

Điều này tránh việc một khoảng thiếu 10 ngày tạo ra nhiệt độ hoặc traffic thay
đổi đều như một đường thẳng giả tạo.

## 11. Làm tròn giống định dạng nguồn

Các cột vốn là số nguyên được lưu không có phần `.0`:

```text
humidity=56
wind_speed=4
traffic_volume=6397
```

Nhiệt độ, mưa và tuyết giữ tối đa hai chữ số thập phân:

```text
temperature=278.18
rain_p_h=0.25
```

## 12. Các cột metadata

Các cột dưới đây **không nằm trong CSV chính**. Chúng nằm trong:

```text
data/processed/TrafficVolumeData_hourly_audit.csv
```

File audit có cột `date_time`, vì vậy có thể nối với CSV chính khi cần kiểm tra:

```python
training_source = hourly_df.merge(
    audit_df,
    on="date_time",
    validate="one_to_one",
)

training_df = training_source[
    training_source["target_observed"]
]
```

Việc tách file giúp CSV chính giống file gốc, nhưng vẫn giữ được thông tin phân
biệt target thật và target suy luận.

### `source_row_count`

Cột này ghi mỗi giờ được tạo từ bao nhiêu dòng gốc.

Ví dụ:

```text
source_row_count=1
```

Giờ bình thường có một dòng.

```text
source_row_count=3
```

Ba dòng CSV được gộp lại.

```text
source_row_count=0
```

Giờ này không tồn tại trong CSV và được chèn.

### `feature_is_imputed`

```text
True = feature của giờ này được suy luận
```

### `feature_imputation_method`

Ghi cách feature được suy luận.

### `imputation_confidence`

Điểm từ 0 đến 1 để mô tả mức tin cậy tương đối:

```text
1.0 = dữ liệu quan sát thật
gần 0.9 = khoảng ngắn hoặc có nhiều mẫu cùng chu kỳ
thấp hơn = ít mẫu tham khảo hơn
```

Đây không phải xác suất đúng tuyệt đối.

### `gap_length_hours`

Cho biết giờ này thuộc đoạn thiếu dài bao nhiêu giờ.

## 13. Báo cáo chất lượng

File:

```text
data/processed/hourly_quality_report.json
```

Nó ghi:

- Khoảng thời gian dữ liệu.
- Số dòng gốc.
- Số giờ duy nhất.
- Số timestamp trùng.
- Số giờ được chèn.
- Số target thật.
- Số target được suy luận.
- Quy tắc điền feature.
- Backtest của quy tắc suy luận target.

Mục đích là giúp ta biết script đã thay đổi dữ liệu như thế nào thay vì chỉ
nhận một file CSV mới mà không có lời giải thích.

### Backtest target suy luận

Script chọn tối đa 500 target thật, tạm che chúng đi rồi yêu cầu thuật toán đoán
lại. Sau đó báo cáo:

```text
MAE
median_absolute_error
p90_absolute_error
MAPE
```

Với CSV hiện tại, phép kiểm tra 500 mẫu cho kết quả gần:

```text
MAE                   khoảng 290 xe
Median absolute error khoảng 150 xe
P90 absolute error    khoảng 634 xe
```

Điều này không chứng minh từng dòng suy luận đều đúng. Nó chỉ cung cấp một phép
đo khách quan hơn việc nhìn dữ liệu bằng mắt.

## 14. Chạy Giai đoạn 1

Từ thư mục dự án:

```powershell
.venv\Scripts\python.exe -m scripts.data.prepare_hourly_data
```

Hoặc khi đã kích hoạt virtual environment:

```powershell
python -m scripts.data.prepare_hourly_data
```

Trên EC2/Docker có Python:

```bash
python -m scripts.data.prepare_hourly_data
```

## 15. Truyền đường dẫn khác

```bash
python -m scripts.data.prepare_hourly_data \
  --input data/raw/TrafficVolumeData_original_2012_2017.csv \
  --output data/processed/custom_hourly.csv \
  --audit data/processed/custom_hourly_audit.csv \
  --report data/processed/custom_report.json
```

## 16. Chạy unit test

```powershell
.venv\Scripts\python.exe -m unittest \
  tests.test_time_series_preprocess -v
```

Các test kiểm tra:

1. Timestamp trùng được gộp.
2. Giờ thiếu được chèn.
3. Target suy luận được gắn cờ, không giả làm target thật.
4. Traffic ưu tiên cùng giờ và cùng thứ.
5. Khoảng thiếu dài không dùng nội suy tuyến tính.
6. Feature số được làm tròn giống nguồn.
7. Target mâu thuẫn được báo cáo.
8. CSV chính giữ đúng schema của CSV gốc.
9. Tên ngày lễ không bị đổi thành `0/1`.
10. CSV chính, CSV audit và JSON được lưu thành công.

## 17. Giới hạn cần hiểu đúng

Dữ liệu suy luận chỉ là ước lượng hợp lý từ lịch sử xung quanh. Nó không thể
biết các sự kiện không có trong CSV, ví dụ:

- Tai nạn bất ngờ.
- Đóng đường.
- Trận đấu hoặc lễ hội.
- Bão cục bộ.
- Lỗi cảm biến.

Vì vậy:

```text
target_observed=True  -> có thể dùng làm nhãn train
target_observed=False -> chỉ dùng tham khảo hoặc tạo history có kiểm soát
```

## 18. Giai đoạn này chưa làm gì?

Chưa làm:

- Lag 1 giờ, 24 giờ hoặc 168 giờ.
- Rolling mean.
- Chuỗi đầu vào cho LSTM.
- Chuẩn hóa scaler.
- Train LightGBM time series.
- Train LSTM.
- Tích hợp API.
- Tích hợp Champion-Challenger.

Đó là các bước sau. Giai đoạn 1 chỉ tạo nền dữ liệu thời gian đúng và có thể
kiểm tra được.

Phần lag và rolling hiện đã được triển khai tại:

```text
docs/phases/GIAI_DOAN_2_FEATURE_TIME_SERIES.md
```
