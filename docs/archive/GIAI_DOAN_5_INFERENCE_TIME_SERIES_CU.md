# Tài liệu lưu trữ: Giai đoạn 5 phiên bản cũ

## 1. Mục tiêu

Giai đoạn 3 đã train được XGBoost và đánh giá bằng bảng feature tạo sẵn.

Giai đoạn 4 trả lời câu hỏi:

> Khi hệ thống dự đoán từng giờ, chỉ biết dữ liệu quá khứ, model còn cho kết
> quả giống lúc đánh giá batch hay không?

Luồng mới:

```text
Biết traffic đến 08:00
        |
        v
Tạo feature để dự đoán 09:00
        |
        v
Dự đoán traffic 09:00
        |
        v
Sau đó mới nhận traffic thật 09:00
        |
        v
Cập nhật lịch sử và dự đoán 10:00
```

Điểm quan trọng là prediction xảy ra trước history update.

## 2. Các file mới

```text
src/time_series_inference.py
backtest_time_series_inference.py
tests/test_time_series_inference.py
```

Kết quả được tạo:

```text
results/time_series_sequential_backtest.csv
results/time_series_sequential_backtest.json
```

## 3. TrafficHistory là gì?

Class:

```python
class TrafficHistory:
```

Đây là bộ nhớ traffic theo giờ. Mỗi giờ lưu:

```text
observed_value  : target thật nếu cảm biến có dữ liệu
target_observed : target có phải dữ liệu thật không
causal_value    : giá trị được phép dùng cho lag
causal_method   : cách tạo causal_value
```

`causal` nghĩa là chỉ dùng thông tin đã xảy ra trong quá khứ.

## 4. Tại sao cần causal value?

Không phải giờ nào cũng có traffic thật. Khi target thiếu, hệ thống không được
nhìn giờ phía sau để điền.

Thứ tự fallback:

```text
1. Target thật của 168 giờ trước.
2. Target thật của 24 giờ trước.
3. Median target thật trong tối đa 168 giờ quá khứ.
4. Causal value gần nhất.
```

Code:

```python
causal_value, method = history.append(
    timestamp,
    traffic_volume,
    target_observed,
)
```

Giá trị `traffic_volume` suy luận offline không được tin là target thật khi
`target_observed=False`.

## 5. Chỉ dự đoán giờ kế tiếp

Nếu lịch sử kết thúc lúc `08:00`, hệ thống chỉ cho dự đoán `09:00`.

```python
expected = history.last_timestamp + pd.Timedelta(hours=1)
```

Yêu cầu dự đoán thẳng `10:00` sẽ báo lỗi vì thiếu trạng thái của `09:00`.

Điều này bảo vệ chuỗi lag khỏi bị đứt.

## 6. Yêu cầu 168 giờ lịch sử

Model dùng:

```text
lag 168 giờ
rolling 168 giờ
```

Vì vậy cần ít nhất 168 giờ liên tục trước target time.

Nếu chưa đủ:

```text
InsufficientHistoryError:
Cần ít nhất 168 giờ lịch sử liên tục.
```

Hệ thống không âm thầm điền số 0 vì việc đó làm dự đoán sai mà khó phát hiện.

## 7. Tạo feature cho một giờ

Hàm:

```python
build_next_hour_feature_row(
    target_time,
    exogenous_features,
    history,
    expected_feature_columns,
)
```

`exogenous_features` là thông tin biết được tại giờ cần dự đoán:

```text
nhiệt độ
độ ẩm
gió
mưa
tuyết
mây
loại thời tiết
ngày lễ
```

`history` cung cấp:

```text
lag 1h, 2h, 3h, 6h, 12h, 24h, 48h, 168h
rolling 3h, 6h, 12h, 24h, 168h
cờ target quan sát
tỉ lệ lịch sử quan sát thật
```

## 8. Kiểm tra hợp đồng 72 feature

Model Giai đoạn 3 chờ đúng 72 feature đầu vào.

Inference kiểm tra:

```python
expected_feature_columns=model.feature_names_in_
```

Nếu thiếu hoặc dư cột, chương trình báo lỗi. Nó không tự thêm số 0 để che giấu
sự khác nhau giữa train và inference.

Test còn so sánh từng feature của một giờ với bảng batch Giai đoạn 2. Kết quả
khớp.

## 9. Dự đoán một giờ

Hàm:

```python
prediction, feature_row = predict_next_hour(
    model,
    target_time,
    exogenous_features,
    history,
)
```

Hàm trả về:

- `prediction`: traffic dự đoán.
- `feature_row`: 72 feature đã đưa vào model để kiểm tra khi cần.

## 10. Backtest tuần tự

Hàm:

```python
run_sequential_backtest(...)
```

Mỗi giờ thực hiện:

```text
1. Lấy thời tiết của giờ hiện tại.
2. Tạo feature từ lịch sử trước giờ hiện tại.
3. Dự đoán.
4. Nếu target thật tồn tại, ghi vào kết quả đánh giá.
5. Cập nhật lịch sử sau cùng.
```

Giờ không có target thật vẫn được xử lý để chuỗi thời gian không bị đứt, nhưng
không được dùng tính MAE.

## 11. Kết quả trên dữ liệu thật

Khoảng backtest:

```text
2014-04-14 16:00:00
đến
2014-08-08 01:00:00
```

Kết quả:

```text
Số giờ lịch đã xử lý       : 2.770
Target thật được đánh giá  : 2.061
Target không quan sát      : 709

MAE  : 178,1967
RMSE : 327,8746
MAPE : 8,8307%
WAPE : 5,1882%
R2   : 0,974166
```

Metric trùng Giai đoạn 3:

```text
Delta MAE  = 0
Delta RMSE = 0
Delta MAPE = 0
Delta WAPE = 0
```

Điều đó chứng minh cách tạo feature batch và tuần tự thống nhất.

Khi so sánh file CSV, chênh lệch prediction lớn nhất khoảng `0,00024` xe. Đây
là sai khác biểu diễn số thực khi ghi/đọc CSV, không phải khác logic.

## 12. Tốc độ inference

Kết quả hiện tại:

```text
Khoảng 48,75 ms cho mỗi giờ lịch
```

Bài toán chỉ dự đoán mỗi giờ một lần nên tốc độ này đủ dùng. Đây chưa phải bài
toán cần hàng nghìn dự đoán mỗi giây.

## 13. Cách chạy

Chạy đủ các giai đoạn:

```powershell
.venv\Scripts\python.exe prepare_time_series_data.py
.venv\Scripts\python.exe prepare_time_series_features.py
.venv\Scripts\python.exe train_time_series_models.py
.venv\Scripts\python.exe backtest_time_series_inference.py
```

Mặc định backtest lấy đúng khoảng test trong:

```text
models/time_series/training_report.json
```

Chọn khoảng khác:

```powershell
.venv\Scripts\python.exe backtest_time_series_inference.py `
  --start "2014-06-01 00:00:00" `
  --end "2014-06-30 23:00:00"
```

## 14. Chạy test

```powershell
.venv\Scripts\python.exe -m unittest tests.test_time_series_inference -v
```

Test kiểm tra:

1. Chưa đủ 168 giờ thì từ chối dự đoán.
2. Chỉ được dự đoán giờ kế tiếp.
3. Lag và rolling chỉ dùng giờ trước.
4. Target thiếu dùng fallback quá khứ.
5. Feature một giờ khớp feature batch.
6. Schema feature khớp model.
7. Lịch sử có khoảng trống bị từ chối.
8. Backtest dự đoán trước rồi mới update.

## 15. Giai đoạn này chưa làm gì?

Chưa làm:

- Thêm endpoint time series vào FastAPI.
- Lưu TrafficHistory bền vững trong Redis hoặc database.
- Nhận traffic thật từ cảm biến production.
- Nhận weather forecast đã kiểm tra timezone.
- Dự báo nhiều giờ tương lai.
- Tích hợp Champion-Challenger.
- Thay thế model production hiện tại.

Model vẫn có trạng thái thử nghiệm:

```text
experimental_not_connected_to_champion_or_api
```

Giai đoạn tiếp theo nên tập trung vào quản lý state và tích hợp có kiểm soát,
không thay champion trực tiếp.
