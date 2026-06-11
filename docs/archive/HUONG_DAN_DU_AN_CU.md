# TÀI LIỆU LƯU TRỮ: HƯỚNG DẪN PHIÊN BẢN CŨ

> Tài liệu này mô tả cấu trúc trước khi dự án được rút gọn. Xem `README.md`
> và `docs/guides/HUONG_DAN_TRAIN_8_MODELS.md` để dùng phiên bản hiện tại.

Tài liệu này dành cho người mới hoàn toàn.

Bạn không cần biết trước:

- Trí tuệ nhân tạo là gì.
- Machine Learning là gì.
- API là gì.
- Docker là gì.
- EC2 là gì.

Mục tiêu của tài liệu không phải bắt bạn học thuộc code. Mục tiêu là giúp bạn
nhìn thấy dữ liệu đi từ đâu, đi qua những bước nào và cuối cùng tạo ra kết quả
gì.

---

> Cập nhật cấu trúc: định nghĩa từng model hiện nằm trong `src/models/`.
> Toàn bộ unit test nằm trong `tests/`. Các phần giải thích `src/train.py`
> bên dưới mô tả API tương thích của pipeline cũ.

# PHẦN 1: BỨC TRANH LỚN

## 1. Dự án trả lời câu hỏi gì?

Dự án muốn trả lời câu hỏi:

> Với ngày giờ và thời tiết cho trước, có khoảng bao nhiêu xe đi qua trong một
> giờ?

Ví dụ:

```text
Thời gian: 08:00 sáng
Nhiệt độ: 20 độ
Trời: có mây
Mưa: không
```

Model có thể trả lời:

```text
Dự đoán: 4.321 xe/giờ
```

Con số này là dự đoán, không phải lời khẳng định chắc chắn.

## 2. Có thể hình dung dự án như một trường học

| Thành phần | Hình dung đơn giản |
|---|---|
| File CSV | Quyển sách chứa bài tập và đáp án cũ |
| Feature | Dữ kiện của đề bài |
| `traffic_volume` | Đáp án cần đoán |
| Model | Học sinh đang học quy luật |
| Training | Quá trình học |
| Validation | Bài kiểm tra để chọn học sinh tốt |
| Test | Bài thi cuối chưa được xem trước |
| Prediction | Đưa đề mới và yêu cầu model trả lời |
| MAE | Trung bình model đoán lệch bao nhiêu xe |
| Drift | Model bắt đầu đoán kém hơn bình thường |
| Retrain | Cho model học lại bằng dữ liệu mới |
| Champion | Model chính thức đang phục vụ người dùng |
| Candidate | Model mới đang chờ thi đấu với Champion |

## 3. Luồng tổng quát của hệ thống

```text
                 HUẤN LUYỆN

TrafficVolumeData.csv
          |
          v
src/preprocess.py
Làm sạch và tạo feature
          |
          v
src/pipeline.py
Chia dữ liệu theo thời gian
          |
          v
src/train.py
Huấn luyện 3 loại model
          |
          v
Chọn model có CV MAE thấp nhất
          |
          v
models/best_model.pkl


                 DỰ ĐOÁN WEB

Người dùng
    |
    v
docs/index.html
    |
    v
POST /predict trong app.py
    |
    v
Open-Meteo cung cấp thời tiết
    |
    v
src/inference.py
    |
    v
models/best_model.pkl
    |
    v
Số xe dự đoán


                 THEO DÕI VÀ RETRAIN

retrain_job.py
    |
    v
Lấy một tháng dữ liệu mới
    |
    v
Champion dự đoán và tính MAE
    |
    v
src/drift.py kiểm tra drift
    |
    +-- Không drift: giữ Champion
    |
    `-- Có drift: huấn luyện Candidate
                       |
                       v
             Cửa sổ tháng tiếp theo
                       |
                       v
          Champion và Candidate cùng làm bài
                       |
                       +-- Candidate tốt hơn đủ 5%: promote
                       |
                       `-- Không đủ tốt: reject
```

---

# PHẦN 2: CẤU TRÚC THƯ MỤC

## 4. Những file quan trọng

```text
traffic-project/
|
|-- app.py
|-- retrain_job.py
|-- main.py
|-- predict.py
|
|-- Dockerfile
|-- docker-compose.yml
|-- requirements.txt
|
|-- data/
|   `-- TrafficVolumeData.csv
|
|-- src/
|   |-- models/
|   |   |-- random_forest.py
|   |   |-- xgboost.py
|   |   |-- lightgbm.py
|   |   `-- recurrent.py
|   |-- preprocess.py
|   |-- train.py
|   |-- pipeline.py
|   |-- inference.py
|   `-- drift.py
|
|-- tests/
|   |-- test_inference.py
|   `-- test_model_lifecycle.py
|
|-- docs/
|   |-- index.html
|   |-- phases/
|   |-- changes/
|   `-- guides/
|
|-- models/
|-- monitoring/
|-- data_versions/
|-- mlruns/
`-- results/
```

## 5. File nào là luồng chính?

Luồng production hiện tại là:

```text
docker-compose.yml
    |
    +-- traffic-api          -> chạy app.py
    |
    `-- traffic-drift-worker -> chạy retrain_job.py
```

Hai file sau là script cũ hoặc script chạy tay:

- `main.py`: luồng train, kiểm tra drift và retrain đơn giản.
- `predict.py`: dự đoán hàng loạt trên một khoảng dữ liệu.

Chúng vẫn có ích để học, nhưng không có đầy đủ cơ chế Champion-Challenger như
`retrain_job.py`.

Khi triển khai bằng Docker Compose, hãy xem `app.py` và `retrain_job.py` là hai
chương trình chính.

---

# PHẦN 3: DỮ LIỆU

## 6. File dữ liệu có dạng gì?

File:

```text
data/TrafficVolumeData.csv
```

Dòng đầu tiên là tên cột:

```csv
date_time,is_holiday,air_pollution_index,humidity,wind_speed,wind_direction,visibility_in_miles,dew_point,temperature,rain_p_h,snow_p_h,clouds_all,weather_type,weather_description,traffic_volume
```

Một dòng dữ liệu:

```csv
2012-10-02 09:00:00,None,121,89,2,329,1,1,288.28,0,0,40,Clouds,scattered clouds,5545
```

## 7. Ý nghĩa các cột

| Cột | Ý nghĩa |
|---|---|
| `date_time` | Ngày và giờ |
| `is_holiday` | Có phải ngày lễ không |
| `air_pollution_index` | Chỉ số ô nhiễm không khí |
| `humidity` | Độ ẩm |
| `wind_speed` | Tốc độ gió |
| `wind_direction` | Hướng gió |
| `visibility_in_miles` | Tầm nhìn xa tính bằng dặm |
| `dew_point` | Điểm sương |
| `temperature` | Nhiệt độ |
| `rain_p_h` | Lượng mưa mỗi giờ |
| `snow_p_h` | Lượng tuyết mỗi giờ |
| `clouds_all` | Mức độ mây che phủ |
| `weather_type` | Nhóm thời tiết |
| `weather_description` | Mô tả chi tiết thời tiết |
| `traffic_volume` | Số xe thật mỗi giờ |

`traffic_volume` là target, tức là đáp án model cần học để dự đoán.

Các cột còn lại chủ yếu là feature, tức là thông tin model dùng để suy nghĩ.

## 8. X và y là gì?

Trong Machine Learning, người ta thường viết:

```text
X = các dữ kiện đầu vào
y = đáp án
```

Trong dự án:

```python
X = dữ liệu thời gian, thời tiết, ô nhiễm...
y = traffic_volume
```

Ví dụ:

```text
X:
  hour = 8
  humidity = 89
  weather = Clouds

y:
  traffic_volume = 5545
```

---

# PHẦN 4: `src/preprocess.py`

## 9. Preprocess nghĩa là gì?

Preprocess là chuẩn bị dữ liệu trước khi model học.

Máy tính không hiểu ngày giờ, chữ `Rain` hay ô trống giống con người. Ta phải
đổi chúng thành các con số và cột rõ ràng.

## 10. Đọc file CSV

```python
def load_data(path):
    df = pd.read_csv(path)
    return df
```

Giải thích:

- `path`: địa chỉ file.
- `pd.read_csv(path)`: đọc file CSV.
- `df`: một bảng dữ liệu Pandas, gọi là DataFrame.
- `return df`: trả bảng về cho nơi gọi hàm.

Ví dụ:

```python
df = load_data("data/TrafficVolumeData.csv")
```

## 11. Xử lý ngày lễ

```python
df["is_holiday"] = df["is_holiday"].notna().astype(int)
```

Ý định của code:

- Ô có tên ngày lễ: đổi thành `1`.
- Ô trống: đổi thành `0`.

`notna()` hỏi:

```text
Ô này có dữ liệu hay không?
```

`astype(int)` đổi:

```text
True  -> 1
False -> 0
```

### Điểm cần cẩn thận

Code này chỉ đúng khi dữ liệu gốc dùng:

```text
None hoặc ô trống -> không phải ngày lễ
Tên ngày lễ       -> là ngày lễ
```

Trong API, `app.py` đang truyền `0` hoặc `1`. Cả số `0` và số `1` đều là ô có
dữ liệu, nên `.notna()` có thể biến cả hai thành `1`.

Đây là hạn chế cần sửa nếu muốn kết quả online đáng tin cậy.

## 12. Đổi chữ ngày giờ thành kiểu thời gian

```python
df["date_time"] = pd.to_datetime(df["date_time"])
```

Trước khi đổi, Python có thể nhìn:

```text
"2012-10-02 09:00:00"
```

như một đoạn chữ.

Sau khi đổi, Pandas hiểu đây là ngày 2 tháng 10 năm 2012 lúc 9 giờ.

## 13. Tách giờ, thứ và tháng

```python
df["hour"] = df["date_time"].dt.hour
df["day"] = df["date_time"].dt.dayofweek
df["month"] = df["date_time"].dt.month
```

Ví dụ:

```text
2012-10-02 09:00:00
```

được tách thành:

```text
hour  = 9
day   = 1
month = 10
```

Pandas quy ước:

```text
Thứ Hai    = 0
Thứ Ba     = 1
...
Thứ Bảy    = 5
Chủ nhật   = 6
```

## 14. Tại sao cần `hour_sin` và `hour_cos`?

```python
df["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24)
df["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24)
```

Giờ trong ngày có dạng vòng tròn:

```text
23 giờ -> 0 giờ -> 1 giờ
```

23 giờ và 0 giờ rất gần nhau. Nhưng nếu chỉ nhìn con số:

```text
23 - 0 = 23
```

máy có thể tưởng chúng rất xa nhau.

Sin và cos giúp biểu diễn giờ như vị trí trên mặt đồng hồ. Người mới chưa cần
thuộc công thức. Chỉ cần nhớ:

> Hai cột này giúp model hiểu thời gian quay vòng sau 24 giờ.

## 15. Tạo cột cuối tuần

```python
df["is_weekend"] = df["day"].isin([5, 6]).astype(int)
```

Nếu ngày là thứ Bảy hoặc Chủ nhật:

```text
is_weekend = 1
```

Nếu là ngày thường:

```text
is_weekend = 0
```

## 16. Đổi chữ thời tiết thành số

```python
df = pd.get_dummies(
    df,
    columns=["weather_type"],
    drop_first=True
)
```

Model thường không học trực tiếp từ chữ:

```text
Clear
Clouds
Rain
Snow
```

Pandas đổi chúng thành các cột bật/tắt:

```text
weather_type_Clouds
weather_type_Rain
weather_type_Snow
```

Ví dụ trời mưa:

```text
weather_type_Clouds = 0
weather_type_Rain   = 1
weather_type_Snow   = 0
```

Cách này gọi là one-hot encoding.

`drop_first=True` bỏ một nhóm làm mốc để giảm cột dư thừa.

## 17. Bỏ cột mô tả và dòng thiếu dữ liệu

```python
df = df.drop(
    ["weather_description"],
    axis=1,
    errors="ignore"
)

df = df.dropna()
```

- `axis=1`: xóa cột.
- `errors="ignore"`: cột không tồn tại cũng không báo lỗi.
- `dropna()`: xóa dòng còn ô trống.

`weather_description` bị bỏ vì dự án chỉ dùng nhóm `weather_type`.

## 18. Chia dữ liệu theo thời gian

```python
df = df.sort_values("date_time")
n = len(df)
train_end = int(n * 0.7)
val_end = int(n * 0.85)
```

Sau đó:

```python
train_df = df[:train_end]
val_df = df[train_end:val_end]
test_df = df[val_end:]
```

Dữ liệu được chia:

```text
70% đầu: train
15% tiếp: validation
15% cuối: test
```

Không xáo trộn ngẫu nhiên vì đây là dữ liệu thời gian. Cách hợp lý là:

```text
Học từ quá khứ -> kiểm tra bằng tương lai
```

chứ không phải:

```text
Học cả dữ liệu tương lai -> quay lại đoán quá khứ
```

---

# PHẦN 5: `src/models/` VÀ `src/train.py`

## 19. File này làm gì?

Định nghĩa model hiện được tách thành:

1. `src/models/random_forest.py`.
2. `src/models/xgboost.py`.
3. `src/models/lightgbm.py`.
4. `src/models/recurrent.py` cho LSTM và GRU.

`src/train.py` chỉ giữ ba hàm tương thích cho pipeline production cũ. Mỗi hàm:

1. Tạo model.
2. Gọi `.fit()` để model học.
3. Trả model đã học.

## 20. Random Forest

```python
model = RandomForestRegressor(
    n_estimators=100,
    random_state=random_state
)
model.fit(X_train, y_train)
```

Random Forest dùng nhiều cây quyết định.

Có thể hình dung:

```text
100 bạn cùng dự đoán -> tổng hợp các câu trả lời
```

- `n_estimators=100`: tạo 100 cây.
- `random_state`: giúp kết quả ngẫu nhiên có thể lặp lại.
- `fit(X_train, y_train)`: học từ dữ kiện và đáp án.

## 21. XGBoost

```python
model = XGBRegressor(
    n_estimators=300,
    max_depth=6,
    learning_rate=0.1,
    subsample=0.8,
    colsample_bytree=0.8,
    random_state=random_state
)
```

XGBoost tạo cây theo từng bước. Cây sau cố sửa lỗi của các cây trước.

| Tham số | Ý nghĩa dễ hiểu |
|---|---|
| `n_estimators=300` | Số cây |
| `max_depth=6` | Cây được phép sâu đến mức nào |
| `learning_rate=0.1` | Mỗi bước sửa mạnh hay nhẹ |
| `subsample=0.8` | Mỗi cây học từ 80% số dòng |
| `colsample_bytree=0.8` | Mỗi cây dùng 80% số cột |

## 22. LightGBM

```python
model = LGBMRegressor(
    n_estimators=300,
    max_depth=-1,
    learning_rate=0.1,
    subsample=0.8,
    colsample_bytree=0.8,
    random_state=random_state
)
```

LightGBM cũng dùng nhiều cây để sửa lỗi dần. Nó thường được dùng vì tốc độ
huấn luyện nhanh và hiệu quả trên dữ liệu dạng bảng.

## 23. `random_state=42` có ý nghĩa gì?

Trong `src/pipeline.py`:

```python
DEFAULT_RANDOM_STATE = 42
```

Một số thuật toán có bước chọn ngẫu nhiên.

Nếu mỗi lần chạy dùng một số khác nhau, kết quả có thể thay đổi. Dùng cố định
`42` giúp:

- Chạy lại dễ ra kết quả giống nhau.
- So sánh các model công bằng hơn.
- Dễ tìm lỗi.

Số `42` không có sức mạnh đặc biệt. Có thể dùng số khác, miễn là giữ cố định
khi so sánh.

---

# PHẦN 6: `src/pipeline.py`

## 24. Pipeline là gì?

Pipeline là dây chuyền nhiều bước:

```text
Đọc dữ liệu
-> xử lý
-> chọn khoảng thời gian
-> chia tập
-> huấn luyện
-> chấm điểm
-> chọn model
-> lưu model
-> ghi MLflow
```

Hàm chính:

```python
def run_pipeline(...):
```

## 25. Ba thước đo MAE, RMSE và MAPE

```python
def evaluate(y_true, y_pred):
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    mape = mean_absolute_percentage_error(y_true, y_pred) * 100
    return mae, rmse, mape
```

### MAE

MAE trả lời:

> Trung bình mỗi lần model đoán lệch bao nhiêu xe?

Ví dụ:

```text
Thật:  1000, 2000, 3000
Đoán: 1100, 1700, 3200
Lỗi:   100,  300,  200

MAE = (100 + 300 + 200) / 3 = 200
```

MAE càng thấp càng tốt.

### RMSE

RMSE cũng đo sai số nhưng phạt các lỗi rất lớn mạnh hơn.

Nếu một lần model đoán sai cực kỳ nhiều, RMSE tăng mạnh.

### MAPE

MAPE trả lời:

> Trung bình model sai khoảng bao nhiêu phần trăm?

MAPE dễ đọc, nhưng cần cẩn thận khi đáp án thật bằng hoặc gần 0.

## 26. Lưu phiên bản dữ liệu

```python
version_name = f"data_v{version_number}"
version_file = f"data_versions/{version_name}.csv"
df.to_csv(version_file, index=False)
```

Mỗi lần train, dự án lưu một bản dữ liệu:

```text
data_v1.csv
data_v2.csv
data_v3.csv
```

Thông tin chung được ghi vào:

```text
data_versions/version_log.csv
```

Mục đích:

> Biết model này đã học từ bộ dữ liệu nào.

## 27. Lưu phiên bản model

```python
model_version = f"model_v{version_number}"
```

Model có các tên:

```text
model_v1
model_v2
model_v3
```

Danh sách phiên bản được ghi vào:

```text
models/model_versions.csv
```

## 28. Lưu JSON an toàn

```python
temporary_path = f"{output_path}.tmp"

with open(temporary_path, "w", encoding="utf-8") as file:
    json.dump(info, file, indent=4, ensure_ascii=False)

os.replace(temporary_path, output_path)
```

Code không ghi trực tiếp vào file chính.

Nó:

1. Ghi vào file tạm `.tmp`.
2. Ghi xong mới thay file chính.

Điều này giảm nguy cơ chương trình khác đọc đúng lúc file mới chỉ được ghi một
nửa.

## 29. Chọn khoảng train

```python
df = df[
    (df["date_time"] >= train_start_date)
    & (df["date_time"] < train_end_date)
].copy()
```

Quy tắc:

```text
train_start_date <= thời gian < train_end_date
```

Ngày bắt đầu được lấy. Thời điểm kết thúc không được lấy.

Ví dụ:

```text
train_start = 2012-11-01
train_end   = 2013-11-01
```

Dữ liệu ngày `2013-11-01` không nằm trong tập train.

## 30. Development set và test set

Pipeline gọi:

```python
train_part, val_part, test_part = split_data(df)
```

Sau đó ghép train và validation:

```python
development_part = pd.concat(
    [train_part, val_part],
    ignore_index=True,
).sort_values("date_time")
```

Development set chiếm khoảng 85% dữ liệu đầu.

Test set là khoảng 15% cuối và được giữ riêng để kiểm tra cuối.

## 31. Tại sao bỏ `traffic_volume` và `date_time` khỏi X?

```python
feature_columns = ["traffic_volume", "date_time"]
X_development = development_part.drop(feature_columns, axis=1)
y_development = development_part["traffic_volume"]
```

`traffic_volume` là đáp án. Nếu để nó trong X, model được nhìn đáp án trước khi
trả lời. Việc này gọi là data leakage.

`date_time` bị bỏ vì các thông tin cần thiết đã được tách thành:

- `hour`
- `day`
- `month`
- `hour_sin`
- `hour_cos`
- `is_weekend`

## 32. TimeSeriesSplit là gì?

```python
time_series_split = TimeSeriesSplit(n_splits=cv_splits)
```

Dự án dùng:

```python
DEFAULT_CV_SPLITS = 3
```

Có thể hình dung ba lần kiểm tra:

```text
Fold 1:
Học [quá khứ 1] -> kiểm tra [đoạn sau 1]

Fold 2:
Học [quá khứ 1 + 2] -> kiểm tra [đoạn sau 2]

Fold 3:
Học [quá khứ 1 + 2 + 3] -> kiểm tra [đoạn sau 3]
```

Điều quan trọng:

> Model luôn học phần trước và kiểm tra bằng phần sau.

Việc chấm nhiều lần ổn định hơn chỉ chấm đúng một lát dữ liệu.

## 33. Cho ba model thi đấu

```python
trainers = [
    ("RandomForest", train_random_forest),
    ("XGBoost", train_xgboost),
    ("LightGBM", train_lightgbm),
]
```

Mỗi model được chạy qua ba fold:

```python
for fold_number, (train_indices, val_indices) in enumerate(
    time_series_split.split(X_development),
    start=1,
):
```

Sau đó lấy trung bình:

```python
mae = float(np.mean([item[0] for item in fold_metrics]))
```

## 34. Vì sao chọn model theo MAE?

```python
best = min(
    results,
    key=lambda result: result["validation_mae"]
)
```

`min()` chọn giá trị nhỏ nhất.

Dự án monitoring cũng dùng MAE để phát hiện drift. Vì vậy, chọn model theo MAE
giúp tiêu chí huấn luyện và tiêu chí vận hành thống nhất.

## 35. Huấn luyện lại model thắng

```python
best_model = best["trainer"](
    X_development,
    y_development,
    random_state,
)
```

Trong các fold, model chỉ học từng phần để được chấm.

Sau khi biết thuật toán thắng, pipeline huấn luyện một model mới bằng toàn bộ
development set.

Sau đó model làm bài test cuối:

```python
test_predictions = best_model.predict(X_test)
test_mae, test_rmse, test_mape = evaluate(
    y_test,
    test_predictions
)
```

## 36. Lưu model

```python
joblib.dump(best_model, output_model_path)
joblib.dump(best_model, versioned_model_path)
```

Ví dụ Champion:

```text
models/best_model.pkl
models/model_v1.pkl
```

Ví dụ Candidate:

```text
models/candidate_model.pkl
models/model_v2.pkl
```

`best_model.pkl` là tên ổn định mà API luôn mở.

`model_vN.pkl` là bản lưu theo phiên bản để theo dõi hoặc quay lại khi cần.

## 37. Metadata model

Pipeline lưu JSON:

```python
model_info = {
    "best_model_name": best["name"],
    "model_version": model_version,
    "model_role": model_role,
    "data_version": data_version,
    "train_start_date": train_start_date,
    "train_end_date": train_end_date,
    ...
}
```

Metadata cho biết:

- Đây là model loại gì.
- Phiên bản bao nhiêu.
- Học từ dữ liệu nào.
- Học khoảng thời gian nào.
- Điểm validation và test.
- File model nằm ở đâu.

## 38. MLflow làm gì?

```python
mlflow.set_tracking_uri("file:./mlruns")
mlflow.set_experiment("Traffic Forecast")
```

MLflow là sổ thí nghiệm.

Nó ghi:

- Model nào được train.
- Tham số nào được dùng.
- MAE, RMSE, MAPE.
- Model role là Champion hay Candidate.
- Data version.
- Model version.

Dữ liệu MLflow hiện được lưu dưới:

```text
mlruns/
```

Dự án chưa khai báo một MLflow server riêng trong Docker Compose.

---

# PHẦN 7: `src/inference.py`

## 39. Inference nghĩa là gì?

Training là học.

Inference là dùng model đã học để dự đoán dữ liệu mới.

## 40. Mở model đã lưu

```python
def load_model():
    model = joblib.load(
        "models/best_model.pkl"
    )
    return model
```

`joblib.load()` mở model Champion hiện tại.

## 41. Đổi một dictionary thành DataFrame

```python
df = pd.DataFrame([raw_input])
```

API gửi một dictionary:

```python
{
    "date_time": "...",
    "humidity": 60,
    ...
}
```

Pandas đổi nó thành bảng một dòng.

## 42. Tại sao thêm target giả?

```python
df["traffic_volume"] = 0
```

`preprocess()` được viết để xử lý bảng giống dữ liệu training, trong đó có cột
`traffic_volume`.

Inference chưa biết traffic thật nên thêm số `0` tạm thời. Sau preprocess, cột
này bị bỏ:

```python
X = df.drop(
    ["traffic_volume", "date_time"],
    axis=1
)
```

Số `0` này không phải kết quả dự đoán.

## 43. Căn chỉnh các cột

```python
X = X.reindex(
    columns=model.feature_names_in_,
    fill_value=0
)
```

Model cần đúng các cột giống lúc học.

Ví dụ lúc học có:

```text
weather_type_Rain
weather_type_Snow
```

Nhưng dữ liệu hiện tại là `Clouds`, nên hai cột trên có thể không được tạo.

`reindex(..., fill_value=0)`:

- Tạo lại các cột bị thiếu.
- Xếp đúng thứ tự.
- Điền `0` cho loại thời tiết không xuất hiện.

## 44. Dự đoán một dòng

```python
pred = model.predict(X)[0]
return float(pred)
```

`model.predict(X)` trả một danh sách kết quả.

Vì chỉ có một dòng nên lấy phần tử đầu tiên bằng `[0]`.

---

# PHẦN 8: `app.py`

## 45. FastAPI là gì?

FastAPI là bộ phận nhận yêu cầu từ trình duyệt.

Nó giống quầy tiếp nhận:

```text
Trình duyệt gửi ngày giờ
-> FastAPI nhận
-> gọi model
-> trả kết quả JSON
```

Ứng dụng được tạo bằng:

```python
app = FastAPI(
    title="Traffic Volume Prediction API"
)
```

## 46. Phục vụ giao diện web

```python
app.mount(
    "/web",
    StaticFiles(directory="docs"),
    name="web"
)
```

Thư mục `docs` được dùng làm static files.

Khi truy cập `/`:

```python
@app.get("/")
def root():
    return FileResponse(
        "docs/index.html"
    )
```

server trả file `docs/index.html`.

## 47. CORS là gì?

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://phuctt205dev.github.io",
        "https://traffic-son.duckdns.org",
        ...
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Trình duyệt có quy tắc bảo vệ không cho website tùy ý gọi server khác.

CORS khai báo những website được phép gọi API.

Nếu frontend và API cùng được phục vụ bởi `app.py`, chúng có cùng origin nên
đơn giản hơn.

## 48. Dữ liệu request

```python
class PredictRequest(BaseModel):
    date_time: str
```

API yêu cầu JSON:

```json
{
  "date_time": "2013-12-01T08:00"
}
```

Nếu thiếu `date_time`, FastAPI sẽ báo request không hợp lệ.

## 49. Đổi mã thời tiết

Open-Meteo trả mã số:

```python
def map_weather_type(code):
    if code == 0:
        return "Clear"
    elif code in [1, 2, 3, 45, 48]:
        return "Clouds"
    ...
```

Dataset lại dùng chữ:

```text
Clear
Clouds
Rain
Snow
```

Hàm này làm nhiệm vụ phiên dịch số thành chữ.

## 50. `get_is_holiday()` đang làm gì thật sự?

```python
def get_is_holiday(date_str):
    dt = pd.to_datetime(date_str)
    return 1 if dt.weekday() >= 5 else 0
```

Tên hàm nói “ngày lễ”, nhưng code thực sự kiểm tra cuối tuần.

Nó trả `1` nếu là:

- Thứ Bảy.
- Chủ nhật.

Nó không biết các ngày lễ như Tết hoặc Quốc khánh.

Đây là điểm cần sửa trong tương lai:

- Đổi tên thành `get_is_weekend()`.
- Hoặc dùng lịch ngày lễ thật của khu vực dữ liệu.

## 51. Gọi Open-Meteo

```python
url = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=44.98"
    "&longitude=-93.26"
    ...
)
```

Tọa độ đang được viết cố định:

```text
latitude  = 44.98
longitude = -93.26
```

Đây là khu vực Minneapolis.

API cần Internet outbound để gọi Open-Meteo.

## 52. Chọn giờ gần nhất

```python
idx = min(
    range(len(api_times)),
    key=lambda i:
    abs(
        (api_times[i] - target_dt).total_seconds()
    )
)
```

Open-Meteo trả một danh sách thời tiết theo giờ.

Code tìm giờ có khoảng cách nhỏ nhất với giờ người dùng chọn.

## 53. Fallback khi Open-Meteo lỗi

```python
except Exception as e:
    return {
        "temperature": 20,
        "humidity": 60,
        ...
    }
```

Nếu mạng lỗi hoặc API lỗi, chương trình dùng giá trị mặc định để không dừng.

Ưu điểm:

- API vẫn trả kết quả.

Nhược điểm:

- Người dùng có thể không biết thời tiết thật không lấy được.
- Dự đoán có thể dựa trên thời tiết giả.

Trong production nghiêm túc, nên trả thêm:

```json
{
  "weather_source": "fallback"
}
```

## 54. Các endpoint

### `GET /health`

```python
@app.get("/health")
def health():
    return {"status": "ok"}
```

Dùng để kiểm tra server còn chạy không.

### `GET /model-info`

```python
@app.get("/model-info")
def model_info():
    return {
        "model_file": "models/best_model.pkl"
    }
```

Hiện endpoint chỉ trả tên file cố định.

Nó chưa đọc:

```text
models/best_model_info.json
```

Do đó chưa trả model version, loại model và điểm test thật.

### `POST /predict`

```python
@app.post("/predict")
def predict(data: PredictRequest):
```

Đây là endpoint chính.

## 55. Luồng bên trong `/predict`

### Bước 1: Lấy thời tiết

```python
weather = get_weather_features(
    data.date_time
)
```

### Bước 2: Tạo payload

```python
payload = {
    "date_time": data.date_time,
    "is_holiday": get_is_holiday(data.date_time),
    "air_pollution_index": 121,
    ...
}
```

### Bước 3: Gọi model

```python
prediction = predict_single(payload)
```

### Bước 4: Trả JSON

```python
return {
    "prediction": int(round(prediction)),
    "features_used": payload
}
```

## 56. Những giới hạn dữ liệu của API hiện tại

### Chỉ số ô nhiễm bị gán cố định

```python
"air_pollution_index": 121
```

API chưa lấy ô nhiễm thật.

### Đơn vị nhiệt độ có khả năng không giống nhau

Dữ liệu training có giá trị:

```text
288.28
```

Giá trị này giống Kelvin hơn độ C.

Open-Meteo mặc định thường trả độ C, ví dụ:

```text
15.13 độ C
```

Mà:

```text
15.13 độ C xấp xỉ 288.28 Kelvin
```

Nếu training dùng Kelvin nhưng API gửi Celsius, model nhận dữ liệu sai đơn vị.

### Ngày ngoài khoảng forecast

Nếu người dùng chọn ngày quá xa khoảng Open-Meteo cung cấp, code vẫn tìm thời
điểm gần nhất trong danh sách thay vì báo ngày không được hỗ trợ.

Ba điểm trên không làm code chắc chắn bị crash, nhưng có thể làm dự đoán không
đáng tin.

---

# PHẦN 9: `docs/index.html`

## 57. HTML, CSS và JavaScript

Một file `index.html` chứa:

- HTML: khung nội dung.
- CSS: màu sắc và cách trình bày.
- JavaScript: hành động khi bấm nút.

## 58. Ô chọn ngày giờ

```html
<input
  type="datetime-local"
  id="date_time"
>
```

`id="date_time"` là tên để JavaScript tìm ô này.

## 59. Nút dự đoán

```html
<button onclick="predict()">
  Predict Traffic
</button>
```

Khi bấm nút, trình duyệt gọi hàm JavaScript:

```javascript
predict()
```

## 60. Lấy giá trị người dùng

```javascript
const dateTime =
  document.getElementById("date_time").value;
```

JavaScript tìm phần tử có id `date_time` và đọc giá trị.

## 61. Tạo JSON gửi API

```javascript
const payload = {
  date_time: dateTime
};
```

Payload có dạng:

```json
{
  "date_time": "2013-12-01T08:00"
}
```

## 62. Gọi `/predict`

```javascript
const response = await fetch(
  "/predict",
  {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  }
);
```

Giải thích:

- `fetch("/predict")`: gọi API.
- `method: "POST"`: gửi dữ liệu lên server.
- `Content-Type`: báo rằng dữ liệu là JSON.
- `JSON.stringify(payload)`: đổi object JavaScript thành chuỗi JSON.
- `await`: chờ server trả lời.

## 63. Đọc kết quả

```javascript
const data = await response.json();
```

Sau đó hiển thị:

```javascript
"Prediction: " +
data.prediction +
" vehicles/hour"
```

## 64. Hiển thị các feature

```javascript
for (const [key, value] of Object.entries(
  data.features_used
)) {
    ...
}
```

Vòng lặp đọc từng feature API đã dùng và tạo một dòng trong bảng HTML.

## 65. Xử lý lỗi giao diện

```javascript
catch (error) {
  document.getElementById("error").innerText =
    "Prediction failed. Please check API connection or server logs.";
}
```

Nếu API không phản hồi hoặc trả lỗi, giao diện hiện thông báo màu đỏ.

---

# PHẦN 10: DRIFT VÀ BASELINE

## 66. Drift là gì?

Model học từ dữ liệu cũ.

Theo thời gian, thế giới có thể thay đổi:

- Thói quen đi lại thay đổi.
- Đường mới được xây.
- Mùa khác nhau.
- Thời tiết khác nhau.
- Sự kiện đặc biệt.
- Chất lượng dữ liệu thay đổi.

Khi model đoán kém hơn mức bình thường, dự án gọi đó là drift.

Trong code hiện tại, drift được nhận biết bằng MAE. Chính xác hơn, đây là
performance drift:

> Model có đang sai nhiều hơn bình thường không?

Code chưa trực tiếp kiểm tra phân bố từng feature có thay đổi không.

## 67. Fixed threshold

Trong Docker Compose:

```yaml
MAE_THRESHOLD: "700"
```

Trong `src/drift.py`:

```python
drift_by_fixed_threshold = (
    current_mae > fixed_threshold
)
```

Nếu:

```text
Current MAE = 750
Fixed threshold = 700
```

thì drift.

## 68. Baseline MAE là gì?

Baseline là mức MAE bình thường gần đây của đúng Champion đang chạy.

Code:

```python
recent_maes = model_history[
    "current_mae"
].tail(history_size)
```

Sau đó:

```python
return float(recent_maes.median())
```

## 69. Median là gì?

Median là số nằm giữa sau khi sắp xếp.

Ví dụ:

```text
300, 320, 340, 350, 900
```

Median:

```text
340
```

Trung bình:

```text
(300 + 320 + 340 + 350 + 900) / 5 = 442
```

Giá trị bất thường `900` kéo trung bình tăng mạnh. Median ít bị ảnh hưởng hơn.

## 70. Baseline chỉ lấy đúng model version

```python
model_history = history[
    history["model_version"].astype(str)
    == str(model_version)
].copy()
```

MAE của `model_v1` không được trộn với MAE của `model_v2`.

Mỗi Champion có lịch sử riêng.

## 71. Loại cửa sổ đã drift khỏi baseline

```python
model_history = model_history[
    model_history["drift"]
    .astype(str)
    .str.lower()
    .isin(stable_values)
]
```

Nếu một tháng đã bị xác định là bất thường, MAE của tháng đó không nên trở
thành định nghĩa mới của “bình thường”.

Nếu không loại nó, baseline có thể phình dần:

```text
MAE xấu -> baseline tăng
-> ngưỡng tăng
-> drift sau khó phát hiện hơn
```

## 72. Vì sao ba lần đầu baseline là `None`?

Docker Compose cấu hình:

```yaml
MIN_BASELINE_WINDOWS: "3"
```

Baseline của cửa sổ hiện tại chỉ được tính từ các cửa sổ trước.

```text
Cửa sổ 1:
  Có 0 MAE cũ -> baseline None

Cửa sổ 2:
  Có 1 MAE cũ -> baseline None

Cửa sổ 3:
  Có 2 MAE cũ -> baseline None

Cửa sổ 4:
  Có đủ 3 MAE cũ -> baseline xuất hiện
```

Ví dụ ba MAE đầu:

```text
360.97, 485.60, 401.65
```

Sắp xếp:

```text
360.97, 401.65, 485.60
```

Median:

```text
401.65
```

## 73. Ratio threshold

```python
ratio_threshold = (
    baseline_mae * float(degradation_ratio)
)
```

Ví dụ:

```text
Baseline = 400
Degradation ratio = 1.2
Ratio threshold = 480
```

Nếu Current MAE lớn hơn 480 thì drift theo tỷ lệ.

### Cấu hình hiện tại cần chú ý

Trong `docker-compose.yml` hiện tại:

```yaml
DEGRADATION_RATIO: "1.0"
```

Điều đó có nghĩa:

```text
Ratio threshold = baseline × 1.0 = baseline
```

Chỉ cần Current MAE lớn hơn baseline một chút là có drift theo tỷ lệ.

Comment trong YAML đang nói “cho phép tăng 20%”, nhưng giá trị `1.0` không phải
20%. Muốn cho phép tăng 20%, cần:

```yaml
DEGRADATION_RATIO: "1.2"
```

Code chạy theo giá trị, không chạy theo comment.

## 74. Quy tắc drift cuối cùng

```python
drift = (
    drift_by_fixed_threshold
    or
    drift_by_degradation_ratio
)
```

Chỉ cần một trong hai đúng:

```text
Current MAE > 700
```

hoặc:

```text
Current MAE > Baseline × Degradation ratio
```

thì drift.

---

# PHẦN 11: `retrain_job.py`

## 75. Worker là gì?

Worker là chương trình chạy nền.

Nó không chờ người dùng bấm nút. Nó tự:

1. Đọc trạng thái.
2. Kiểm tra một cửa sổ dữ liệu.
3. Tính MAE.
4. Phát hiện drift.
5. Train Candidate nếu cần.
6. Ngủ.
7. Lặp lại.

## 76. Cấu hình từ environment

```python
MAE_THRESHOLD = float(
    os.getenv("MAE_THRESHOLD", "700")
)
```

Ý nghĩa:

- Nếu Docker truyền `MAE_THRESHOLD`, dùng giá trị Docker.
- Nếu không có, dùng mặc định `700`.

Docker Compose đang truyền hầu hết cấu hình.

## 77. Các cấu hình quan trọng

| Biến | Ý nghĩa |
|---|---|
| `DATA_PATH` | File dữ liệu |
| `MODEL_PATH` | Champion |
| `CANDIDATE_MODEL_PATH` | Candidate đang chờ đánh giá |
| `STATE_PATH` | Trí nhớ của worker |
| `DRIFT_HISTORY_PATH` | Lịch sử drift |
| `PROMOTION_HISTORY_PATH` | Lịch sử thi Champion-Candidate |
| `MAE_THRESHOLD` | Ngưỡng MAE cứng |
| `DEGRADATION_RATIO` | Hệ số ngưỡng tương đối |
| `BASELINE_HISTORY_WINDOWS` | Số cửa sổ gần nhất dùng tính median |
| `MIN_BASELINE_WINDOWS` | Tối thiểu bao nhiêu cửa sổ mới có baseline |
| `MIN_PROMOTION_IMPROVEMENT` | Candidate cần tốt hơn bao nhiêu |
| `CHECK_WINDOW_MONTHS` | Mỗi lần kiểm tra bao nhiêu tháng |
| `TRAIN_WINDOW_MONTHS` | Candidate học từ bao nhiêu tháng gần nhất |
| `CHECK_INTERVAL_SECONDS` | Nghỉ bao lâu giữa hai lần kiểm tra |
| `RUN_ONCE` | Chạy một vòng rồi dừng hay chạy mãi |

## 78. Worker có trí nhớ

State mặc định:

```python
{
    "next_check_start": DRIFT_START_DATE,
    "model_train_start": INITIAL_TRAIN_START,
    "model_train_end": INITIAL_TRAIN_END,
    "last_check_start": None,
    "last_check_end": None,
    "last_drift": None,
    "last_mae": None,
    ...
}
```

File:

```text
monitoring/drift_state.json
```

Worker ghi nhớ:

- Lần tiếp theo bắt đầu từ tháng nào.
- Lần trước MAE bao nhiêu.
- Có drift không.
- Candidate lần trước ra sao.
- Đã chạy bao nhiêu lần.
- Lỗi gần nhất.

Nếu container restart, worker đọc state và tiếp tục.

## 79. Tạo Champion đầu tiên

```python
if os.path.exists(MODEL_PATH):
    return
```

Nếu `best_model.pkl` đã có, không train lại.

Nếu chưa có:

```python
run_pipeline(
    train_start_date=INITIAL_TRAIN_START,
    train_end_date=INITIAL_TRAIN_END,
    output_model_path=MODEL_PATH,
    output_info_path=MODEL_INFO_PATH,
    model_role="champion",
)
```

Model đầu tiên được lưu trực tiếp làm Champion.

## 80. Rolling train window

```python
train_end = pd.to_datetime(check_end)
train_start = (
    train_end
    - pd.DateOffset(months=TRAIN_WINDOW_MONTHS)
)
```

Nếu:

```text
TRAIN_WINDOW_MONTHS = 12
check_end = 2014-03-01
```

thì Candidate học:

```text
2013-03-01 -> 2014-03-01
```

Cửa sổ luôn dịch chuyển theo thời gian.

## 81. Champion và Candidate

### Champion

Model chính thức:

```text
models/best_model.pkl
```

API dùng file này để dự đoán.

### Candidate

Model mới:

```text
models/candidate_model.pkl
```

Candidate chưa được phép thay Champion ngay.

## 82. Tại sao không promote Candidate ngay sau retrain?

Candidate vừa học đến `check_end`.

Nếu lập tức chấm Candidate bằng dữ liệu nó vừa học, bài kiểm tra không công
bằng.

Hệ thống chờ cửa sổ tiếp theo:

```text
Tháng hiện tại:
  phát hiện drift
  -> train Candidate

Tháng tiếp theo:
  Champion và Candidate cùng dự đoán
  -> so MAE trên cùng dữ liệu chưa thấy
```

## 83. Căn chỉnh feature cho từng model

```python
if hasattr(model, "feature_names_in_"):
    features = features.reindex(
        columns=model.feature_names_in_,
        fill_value=0,
    )
```

Champion và Candidate có thể đã học ở các thời điểm khác nhau. Các loại thời
tiết xuất hiện trong dữ liệu training có thể khác nhau.

Mỗi model được nhận đúng bộ cột của chính nó.

## 84. Tính mức cải thiện

```python
return (
    champion_mae - candidate_mae
) / champion_mae
```

Ví dụ:

```text
Champion MAE = 500
Candidate MAE = 470

Improvement = (500 - 470) / 500
            = 0.06
            = 6%
```

## 85. Điều kiện promote

Docker Compose:

```yaml
MIN_PROMOTION_IMPROVEMENT: "0.05"
```

Code:

```python
return (
    improvement >= minimum_improvement,
    improvement
)
```

Candidate phải giảm MAE ít nhất 5%.

Ví dụ:

```text
Champion MAE = 500

Candidate = 470 -> tốt hơn 6% -> promote
Candidate = 490 -> tốt hơn 2% -> reject
Candidate = 520 -> tệ hơn      -> reject
```

## 86. Promote an toàn

```python
temporary_model_path = f"{MODEL_PATH}.tmp"
shutil.copy2(
    CANDIDATE_MODEL_PATH,
    temporary_model_path
)
os.replace(
    temporary_model_path,
    MODEL_PATH
)
```

Candidate được sao chép vào file tạm.

Chỉ khi sao chép xong mới thay Champion.

Mục đích:

> Tránh API mở phải file model đang được ghi dở.

## 87. Promotion history

Mỗi cuộc thi được ghi vào:

```text
monitoring/promotion_history.csv
```

Thông tin gồm:

- Champion version.
- Candidate version.
- Champion MAE.
- Candidate MAE.
- Tỷ lệ cải thiện.
- Quyết định `promoted` hoặc `rejected`.

## 88. `check_drift_once()` chạy như thế nào?

### Bước 1

Tạo thư mục cần thiết:

```python
ensure_folders()
```

### Bước 2

Tạo Champion nếu chưa có:

```python
train_initial_model_if_missing()
```

### Bước 3

Đọc state:

```python
state = load_state()
```

### Bước 4

Tạo cửa sổ kiểm tra:

```python
check_end = (
    check_start
    + pd.DateOffset(months=CHECK_WINDOW_MONTHS)
)
```

### Bước 5

Lấy dữ liệu trong cửa sổ:

```python
current_window = dataframe[
    (dataframe["date_time"] >= check_start)
    & (dataframe["date_time"] < check_end)
].copy()
```

### Bước 6

Nếu có Candidate từ lần trước, cho Candidate thi với Champion:

```python
comparison = evaluate_pending_candidate(
    champion_model,
    current_window,
)
```

### Bước 7

Tính baseline từ lịch sử Champion:

```python
baseline_mae = get_historical_mae_baseline(...)
```

### Bước 8

Kiểm tra drift:

```python
drift, current_mae = detect_drift_by_mae(...)
```

### Bước 9

Nếu drift, train Candidate:

```python
if drift:
    train_candidate(
        candidate_train_start,
        candidate_train_end
    )
```

### Bước 10

Cập nhật cửa sổ tiếp theo:

```python
state["next_check_start"] = to_date_string(
    check_end
)
```

## 89. Vòng lặp vô hạn

```python
while True:
    check_drift_once()
    ...
    time.sleep(CHECK_INTERVAL_SECONDS)
```

Worker kiểm tra xong thì ngủ.

Trong Compose hiện tại:

```yaml
CHECK_INTERVAL_SECONDS: "30"
```

Đây là tốc độ demo:

```text
30 giây mô phỏng một tháng dữ liệu
```

Không phải ngoài đời cứ 30 giây cần retrain.

Production thật có thể kiểm tra theo giờ, ngày hoặc khi dữ liệu tháng đã đủ.

---

# PHẦN 12: CÁC FILE TEST

## 90. `test_inference.py`

File tạo một input mẫu:

```python
sample = {
    "date_time": "2013-12-01 08:00:00",
    "humidity": 89,
    ...
}
```

Sau đó gọi:

```python
result = predict_single(sample)
```

Mục đích:

> Kiểm tra model có thể nhận một dòng và trả dự đoán không.

File cần `models/best_model.pkl` tồn tại.

## 91. `test_model_lifecycle.py`

Đây là unit test.

Nó không chạy trong API và không chạy trong worker production.

Nó chỉ chạy khi bạn chủ động gọi:

```bash
python -m unittest tests.test_model_lifecycle -v
```

### Kiểm tra baseline

```python
self.assertEqual(
    baseline,
    110.0
)
```

Test xác nhận:

- Chỉ lấy đúng model version.
- Bỏ cửa sổ đã drift.
- Dùng median.

### Kiểm tra chưa đủ lịch sử

```python
self.assertIsNone(baseline)
```

Nếu chỉ có hai cửa sổ mà yêu cầu ba, baseline phải là `None`.

### Kiểm tra promote

```python
self.assertTrue(promote)
```

Candidate tốt hơn đủ 5% thì phải promote.

### Kiểm tra reject

```python
self.assertFalse(promote)
```

Candidate chỉ tốt hơn 2% thì không được promote.

### Kiểm tra thay Champion

Test tạo model giả trong thư mục tạm, chạy promotion rồi kiểm tra:

```python
self.assertTrue(result["promoted"])
self.assertFalse(
    os.path.exists(candidate_path)
)
```

Nó không đụng model thật trong `models/`.

## 92. Tại sao nên giữ test trong dự án?

Test giống bài tự chấm.

Khi sửa code, bạn chạy test để biết:

- Baseline còn đúng không.
- Quy tắc 5% còn đúng không.
- Promotion có làm hỏng file không.
- Chức năng cũ có bị vô tình phá không.

---

# PHẦN 13: `main.py` VÀ `predict.py`

## 93. `main.py`

`main.py` là luồng đơn giản, cấu hình viết trực tiếp trong code:

```python
TRAIN_START_DATE = "2013-01-01"
TRAIN_END_DATE = "2013-06-01"
PREDICT_START = "2013-12-01"
PREDICT_END = "2014-01-01"
MAE_THRESHOLD = 100
```

Nó:

1. Train model nếu chưa có.
2. Dự đoán một tháng.
3. Tính drift.
4. Retrain trực tiếp nếu drift.

Nó không có:

- Baseline median production.
- Candidate chờ promotion.
- So Champion và Candidate.
- State chạy nền.

Do đó, khi học production workflow, ưu tiên `retrain_job.py`.

## 94. `predict.py`

`predict.py`:

1. Mở Champion.
2. Lọc một khoảng thời gian.
3. Dự đoán hàng loạt.
4. Tính MAE, RMSE, MAPE.
5. Lưu `results/predict.csv`.
6. Lưu `results/predict_log.csv`.
7. Vẽ biểu đồ bằng Matplotlib.

Điểm cần chú ý:

```python
plt.show()
```

Lệnh này mở cửa sổ biểu đồ. Trên server EC2 không có màn hình, nó có thể không
phù hợp. Trên server nên dùng:

```python
plt.savefig("results/prediction.png")
```

thay vì chỉ `plt.show()`.

---

# PHẦN 14: DOCKER

## 95. Docker là gì?

Docker đóng gói:

- Python.
- Thư viện.
- Code.
- Lệnh khởi động.

thành một môi trường nhất quán gọi là container.

Nhờ đó:

```text
Máy cá nhân chạy được
EC2 cũng chạy gần giống như vậy
```

## 96. Dockerfile

```dockerfile
FROM python:3.11
```

Dùng image nền có Python 3.11.

```dockerfile
WORKDIR /app
```

Chọn `/app` làm thư mục làm việc trong container.

```dockerfile
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
```

Chép danh sách thư viện và cài chúng.

```dockerfile
COPY . .
```

Chép source code vào image.

```dockerfile
EXPOSE 8000
```

Ghi chú ứng dụng dùng cổng 8000.

```dockerfile
CMD [
  "uvicorn",
  "app:app",
  "--host",
  "0.0.0.0",
  "--port",
  "8000"
]
```

Lệnh mặc định chạy FastAPI.

## 97. Docker Compose là gì?

Docker Compose đọc file YAML và chạy nhiều service cùng nhau.

Dự án có hai service:

```yaml
services:
  traffic-api:
  traffic-drift-worker:
```

## 98. Service API

```yaml
traffic-api:
  build: .
  container_name: traffic-mlops-container
```

Docker build image từ Dockerfile hiện tại.

```yaml
ports:
  - "8000:8000"
```

Ý nghĩa:

```text
Cổng 8000 EC2 -> cổng 8000 container
```

## 99. Service worker

```yaml
traffic-drift-worker:
  build: .
  command: python -u retrain_job.py
```

Nó dùng cùng image với API nhưng đổi lệnh khởi động.

- API chạy Uvicorn.
- Worker chạy `retrain_job.py`.

`-u` làm log Python được in ngay, thuận tiện khi xem `docker logs`.

## 100. Environment trong Compose

```yaml
environment:
  MAE_THRESHOLD: "700"
  CHECK_WINDOW_MONTHS: "1"
  TRAIN_WINDOW_MONTHS: "12"
```

Các giá trị này đi vào:

```python
os.getenv(...)
```

Nếu giá trị Docker khác giá trị mặc định Python, Docker thắng.

## 101. Volume

```yaml
volumes:
  - ./models:/app/models
  - ./monitoring:/app/monitoring
  - ./data:/app/data
```

Bên trái là thư mục trên EC2:

```text
./models
```

Bên phải là thư mục trong container:

```text
/app/models
```

Hai bên nhìn chung một dữ liệu.

Nhờ volume:

- API và worker dùng chung Champion.
- Xóa container không làm mất model.
- Log drift còn trên EC2.
- State còn sau khi restart.

## 102. Restart policy

```yaml
restart: unless-stopped
```

Nếu container dừng bất ngờ, Docker cố khởi động lại.

Nếu bạn chủ động stop container, Docker không tự bật nó ngay.

Docker daemon cũng cần được bật cùng hệ điều hành để chính sách này hữu ích sau
khi EC2 reboot.

---

# PHẦN 15: CHẠY TRÊN MÁY CÁ NHÂN

## 103. Chạy bằng Python

Tạo môi trường:

```powershell
python -m venv .venv
```

Kích hoạt trên Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Cài thư viện:

```powershell
pip install -r requirements.txt
```

Chạy API:

```powershell
uvicorn app:app --host 0.0.0.0 --port 8000
```

Mở:

```text
http://localhost:8000/
http://localhost:8000/health
http://localhost:8000/docs
```

`/docs` là Swagger UI tự động của FastAPI.

## 104. Chạy bằng Docker Compose

Kiểm tra cấu hình:

```powershell
docker compose config
```

Build và chạy:

```powershell
docker compose up --build
```

Chạy nền:

```powershell
docker compose up --build -d
```

Xem container:

```powershell
docker compose ps
```

Xem log:

```powershell
docker compose logs -f
```

Dừng:

```powershell
docker compose down
```

`docker compose down` xóa container và network Compose, nhưng các bind-mounted
folder như `models/` và `monitoring/` vẫn nằm trên máy.

---

# PHẦN 16: TRIỂN KHAI TRÊN AWS EC2

## 105. EC2 là gì?

EC2 là một máy tính chạy trên AWS.

Thay vì đặt máy ở nhà, AWS cung cấp:

- CPU.
- RAM.
- Ổ đĩa.
- Địa chỉ mạng.
- Hệ điều hành.

Bạn kết nối từ xa bằng SSH và chạy Docker trên đó.

## 106. Kiến trúc trên EC2

```text
Máy người dùng
      |
      | HTTP cổng 8000
      v
EC2 Security Group
      |
      v
Cổng 8000 của EC2
      |
      v
traffic-api container
      |
      +---- docs/index.html
      +---- FastAPI
      +---- best_model.pkl
      `---- Open-Meteo qua Internet outbound


EC2
 |
 `-- traffic-drift-worker container
       |
       +---- đọc TrafficVolumeData.csv
       +---- đọc/ghi monitoring/
       +---- train Candidate
       `---- có thể thay Champion
```

## 107. Tạo EC2

Các bước trên AWS Console:

1. Chọn **Launch instance**.
2. Chọn Ubuntu Server.
3. Chọn instance có đủ RAM cho Pandas, XGBoost, LightGBM và MLflow.
4. Tạo hoặc chọn key pair.
5. Tạo storage đủ chứa dataset, Docker image, model và MLflow artifacts.
6. Gắn Security Group.
7. Launch instance.

Model training dùng nhiều RAM hơn API. Nếu instance quá nhỏ:

- Build có thể chậm.
- Train có thể bị hệ điều hành dừng.
- Container có thể restart do thiếu bộ nhớ.

Không có một loại instance duy nhất đúng cho mọi trường hợp. Hãy xem RAM thực
tế bằng:

```bash
free -h
```

và tài nguyên container bằng:

```bash
docker stats
```

## 108. Security Group

Tối thiểu trong giai đoạn thử nghiệm:

| Port | Mục đích | Source nên dùng |
|---|---|---|
| 22 | SSH | Chỉ IP của bạn |
| 8000 | Web/API demo | IP của bạn hoặc phạm vi cần thiết |

Không nên mở SSH `22` cho toàn Internet nếu không cần.

AWS khuyến nghị giới hạn cổng SSH theo địa chỉ IP cụ thể:

```text
https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/changing-security-group.html
```

Nếu triển khai web thật:

- Mở `80` cho HTTP.
- Mở `443` cho HTTPS.
- Đặt Nginx, Caddy, Application Load Balancer hoặc reverse proxy phía trước.
- Không nhất thiết công khai cổng `8000`.

Repo hiện tại chưa có cấu hình Nginx hoặc HTTPS.

## 109. Kết nối SSH

Trên máy cá nhân:

```bash
ssh -i your-key.pem ubuntu@EC2_PUBLIC_IP
```

Thay:

- `your-key.pem`: file key của bạn.
- `EC2_PUBLIC_IP`: Public IPv4 của EC2.

Nếu báo permission của key quá rộng trên Linux/macOS:

```bash
chmod 400 your-key.pem
```

## 110. Cài Git và Docker

Cập nhật danh sách package:

```bash
sudo apt update
```

Cài Git:

```bash
sudo apt install -y git
```

Docker thay đổi cách cài theo phiên bản Ubuntu. Nên dùng hướng dẫn chính thức:

```text
https://docs.docker.com/engine/install/ubuntu/
```

Sau khi đã cấu hình repository Docker chính thức, các package chính gồm:

```bash
sudo apt install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin
```

Kiểm tra:

```bash
docker --version
docker compose version
```

Bật Docker khi máy khởi động:

```bash
sudo systemctl enable --now docker
```

Cho user `ubuntu` dùng Docker không cần `sudo`:

```bash
sudo usermod -aG docker ubuntu
```

Sau lệnh này, đăng xuất SSH rồi đăng nhập lại.

Docker có tài liệu cài Compose chính thức:

```text
https://docs.docker.com/compose/install/
```

## 111. Đưa code lên EC2

Nếu code nằm trên Git:

```bash
git clone YOUR_REPOSITORY_URL
cd YOUR_PROJECT_FOLDER
```

Ví dụ cấu trúc đúng sau khi `cd`:

```bash
ls
```

phải thấy:

```text
app.py
retrain_job.py
docker-compose.yml
Dockerfile
src
data
docs
```

## 112. Kiểm tra trước khi chạy

Kiểm tra dataset:

```bash
ls -lh data/TrafficVolumeData.csv
```

Kiểm tra Compose:

```bash
docker compose config
```

Nếu lệnh này lỗi, sửa YAML trước khi build.

## 113. Build và chạy

```bash
docker compose up --build -d
```

Ý nghĩa:

- `up`: tạo và chạy service.
- `--build`: build lại image.
- `-d`: chạy nền.

Docker Compose được Docker hỗ trợ cho triển khai trên một server. Tài liệu:

```text
https://docs.docker.com/compose/how-tos/production/
```

## 114. Kiểm tra container

```bash
docker compose ps
```

Bạn cần thấy hai service:

```text
traffic-api
traffic-drift-worker
```

Hoặc container name:

```text
traffic-mlops-container
traffic-drift-worker-container
```

## 115. Xem log API

```bash
docker logs -f traffic-mlops-container
```

Hoặc:

```bash
docker compose logs -f traffic-api
```

## 116. Xem log worker

```bash
docker logs -f traffic-drift-worker-container
```

Hoặc:

```bash
docker compose logs -f traffic-drift-worker
```

Nhấn `Ctrl+C` chỉ thoát khỏi màn hình xem log. Container vẫn chạy nền.

## 117. Kiểm tra health ngay trên EC2

```bash
curl http://localhost:8000/health
```

Kết quả mong đợi:

```json
{"status":"ok"}
```

## 118. Mở từ máy cá nhân

```text
http://EC2_PUBLIC_IP:8000/
```

Health:

```text
http://EC2_PUBLIC_IP:8000/health
```

Swagger:

```text
http://EC2_PUBLIC_IP:8000/docs
```

Nếu `curl localhost` trên EC2 chạy nhưng trình duyệt bên ngoài không vào được,
hãy kiểm tra:

1. Security Group có mở TCP 8000 không.
2. EC2 có Public IPv4 không.
3. Container có trạng thái running không.
4. Compose có map `"8000:8000"` không.

## 119. Vì sao API và worker nhìn thấy cùng model?

Cả hai service đều mount:

```yaml
- ./models:/app/models
```

Khi worker promote Candidate:

```text
EC2: ./models/best_model.pkl
```

được cập nhật.

API mở:

```text
/app/models/best_model.pkl
```

Hai đường dẫn đó là cùng một file qua volume.

## 120. Dữ liệu nào tồn tại sau restart?

Các thư mục bind mount:

```text
models/
results/
monitoring/
mlruns/
data_versions/
data/
```

chứa dữ liệu trên ổ đĩa EC2.

Restart container không xóa chúng.

Nhưng nếu xóa EC2 hoặc xóa EBS volume thì dữ liệu có thể mất.

Nên sao lưu:

- `models/`
- `monitoring/`
- `data_versions/`
- Dữ liệu nguồn quan trọng.

## 121. `.gitignore` và dữ liệu production

`.gitignore` hiện bỏ qua:

```text
data_versions/
monitoring/
models/*.csv
models/*.pkl
results/
mlruns/
```

Điều đó có nghĩa:

- Git thường không tải model production lên repository.
- `git pull` không thay lịch sử drift trên EC2.
- Model và log được giữ riêng trên server.

Đây là lý do cần backup server, không thể chỉ dựa vào Git.

## 122. Cập nhật code trên EC2

Vào thư mục dự án:

```bash
cd YOUR_PROJECT_FOLDER
```

Lấy code mới:

```bash
git pull
```

Kiểm tra Compose:

```bash
docker compose config
```

Build và chạy lại:

```bash
docker compose up --build -d
```

Xem log:

```bash
docker compose logs -f --tail=100
```

## 123. Dừng hệ thống

```bash
docker compose down
```

Dừng mà không xóa container:

```bash
docker compose stop
```

Chạy lại:

```bash
docker compose start
```

## 124. EC2 reboot

Kiểm tra Docker:

```bash
sudo systemctl status docker
```

Kiểm tra container:

```bash
docker compose ps
```

Vì Compose có:

```yaml
restart: unless-stopped
```

container thường tự chạy lại khi Docker daemon khởi động, trừ khi trước đó bạn
chủ động stop chúng.

## 125. Domain và HTTPS

Repo có CORS cho:

```text
https://traffic-son.duckdns.org
```

Nhưng chỉ thêm CORS không tự tạo:

- Domain.
- DNS.
- HTTPS certificate.
- Reverse proxy.

Muốn dùng domain thật cần:

1. Domain trỏ DNS về EC2 hoặc Load Balancer.
2. Nginx/Caddy/ALB nhận cổng 80 và 443.
3. TLS certificate.
4. Proxy request vào `localhost:8000`.

Đây là hạ tầng bên ngoài code hiện tại.

## 126. Lưu ý về public IP

Public IPv4 thông thường của EC2 có thể thay đổi khi stop/start instance.

Nếu cần địa chỉ ổn định:

- Dùng Elastic IP.
- Hoặc đặt Load Balancer/domain phù hợp.

## 127. Lưu ý chi phí

Các thành phần có thể phát sinh phí:

- EC2 instance.
- EBS storage.
- Public IPv4.
- Data transfer.
- Elastic IP trong một số trạng thái sử dụng.
- Load Balancer nếu dùng.

Theo dõi AWS Billing và đặt Budget Alert.

---

# PHẦN 17: ĐỌC LOG WORKER

## 128. Log train ban đầu

Ví dụ:

```text
No champion model found.
Training champion model...
Random state: 42
```

Nghĩa là chưa có `best_model.pkl`, worker bắt đầu train.

## 129. Log ba model

```text
RandomForest: CV MAE=396.13
XGBoost: CV MAE=405.59
LightGBM: CV MAE=380.99
```

MAE nhỏ nhất là LightGBM:

```text
380.99
```

Pipeline chọn LightGBM.

## 130. Log test cuối

```text
Best validation-MAE model: LightGBM
Test metrics: MAE=328.73
```

Điều này có nghĩa:

- LightGBM thắng ở cross-validation.
- Model được train lại trên development set.
- Model đạt MAE 328.73 trên test cuối.

## 131. Log baseline chưa xuất hiện

```text
Baseline MAE      : None
Ratio threshold   : Disabled
```

Không phải lỗi nếu chưa đủ `MIN_BASELINE_WINDOWS`.

Ngưỡng cứng vẫn chạy:

```text
Fixed threshold: 700
```

## 132. Log không drift

```text
Current MAE       : 372.61
Baseline MAE      : 401.65
Ratio threshold   : 401.65
Drift by fixed    : False
Drift by ratio    : False
NO DRIFT
```

Với `DEGRADATION_RATIO=1.0`, ratio threshold bằng baseline.

Current MAE thấp hơn baseline nên không drift.

## 133. Log drift

```text
Current MAE       : 500
Baseline MAE      : 400
Ratio threshold   : 400
Drift by ratio    : True
DRIFT DETECTED
```

Worker sẽ train Candidate bằng rolling window.

## 134. Log Champion-Candidate

```text
Champion MAE: 500
Candidate MAE: 460
Improvement: 8.00%
Candidate promoted to champion
```

Candidate tốt hơn 8%, vượt yêu cầu 5%, nên được promote.

Nếu:

```text
Champion MAE: 500
Candidate MAE: 490
Improvement: 2.00%
Candidate rejected
```

Champion cũ được giữ.

---

# PHẦN 18: KIỂM TRA VÀ GỠ LỖI

## 135. API không lên

Kiểm tra:

```bash
docker compose ps
docker compose logs traffic-api
curl http://localhost:8000/health
```

Các nguyên nhân thường gặp:

- Build thư viện lỗi.
- Thiếu model.
- Port 8000 đang bị chương trình khác dùng.
- File hoặc thư mục không tồn tại.

## 136. Worker restart liên tục

```bash
docker compose logs --tail=200 traffic-drift-worker
```

Kiểm tra:

- Dataset có tồn tại không.
- State JSON có hỏng không.
- Ổ đĩa còn chỗ không.
- RAM có đủ không.
- MLflow có lỗi quyền ghi không.

## 137. Kiểm tra dung lượng ổ đĩa

```bash
df -h
```

Các thư mục tăng theo thời gian:

- `mlruns/`
- `data_versions/`
- `models/`
- `monitoring/`
- Docker images.

Kiểm tra Docker:

```bash
docker system df
```

Không nên xóa bừa model hoặc volume production khi chưa backup.

## 138. Kiểm tra RAM

```bash
free -h
docker stats
```

Nếu container bị kill khi train, thiếu RAM là một khả năng.

## 139. Open-Meteo lỗi

Log API có thể hiện:

```text
Weather API FAILED
```

Kiểm tra EC2 có Internet outbound:

```bash
curl https://api.open-meteo.com/
```

Security Group outbound hoặc route của subnet phải cho phép truy cập Internet.

## 140. Model không tồn tại

```bash
ls -lh models/
```

Worker có nhiệm vụ tạo Champion nếu thiếu.

Nếu chỉ chạy service API mà worker chưa tạo model, `/predict` có thể lỗi khi
`joblib.load()` không tìm thấy `best_model.pkl`.

## 141. Xem state

```bash
cat monitoring/drift_state.json
```

Các trường đáng chú ý:

```text
next_check_start
last_status
last_mae
last_baseline_mae
last_drift
last_error
```

## 142. Xem drift history

```bash
tail -n 20 monitoring/drift_history.csv
```

## 143. Xem promotion history

```bash
tail -n 20 monitoring/promotion_history.csv
```

File này chỉ xuất hiện sau khi có Candidate được đem ra so sánh.

---

# PHẦN 19: NHỮNG ĐIỂM NÊN CẢI THIỆN

## 144. Sửa `is_holiday`

Không để số `0` bị biến thành `1`.

Nên tách:

- Xử lý dữ liệu CSV dạng tên ngày lễ.
- Xử lý input API đã là số.

## 145. Chuẩn hóa đơn vị nhiệt độ

Xác nhận dataset dùng Kelvin hay Celsius.

Sau đó đổi Open-Meteo về cùng đơn vị trước khi predict.

## 146. Lấy ô nhiễm thật

Thay:

```python
"air_pollution_index": 121
```

bằng API dữ liệu ô nhiễm phù hợp.

## 147. Kiểm tra ngày Open-Meteo hỗ trợ

Nếu ngày người dùng chọn nằm ngoài forecast, trả lỗi rõ ràng thay vì lấy giờ
gần nhất không đúng ngày.

## 148. Cải thiện `/model-info`

Đọc:

```text
models/best_model_info.json
```

và trả:

- Model version.
- Loại model.
- Train window.
- Test MAE.
- Promotion metrics.

## 149. Cache model trong API

Hiện mỗi request gọi:

```python
joblib.load("models/best_model.pkl")
```

Việc mở model lại mỗi lần tốn thời gian.

Có thể cache model và reload khi file thay đổi.

## 150. Tách cấu hình khỏi code

Các giá trị trong `app.py` đang viết cứng:

- Tọa độ.
- Air pollution.
- Fallback weather.
- CORS origins.

Nên đưa chúng sang environment.

## 151. Sửa comment và giá trị `DEGRADATION_RATIO`

Comment hiện nói cho phép tăng 20%, nhưng YAML đang là:

```yaml
DEGRADATION_RATIO: "1.0"
```

Cần chọn một ý định rõ:

```yaml
# Drift ngay khi vượt baseline
DEGRADATION_RATIO: "1.0"
```

hoặc:

```yaml
# Cho phép tăng 20%
DEGRADATION_RATIO: "1.2"
```

## 152. Thêm healthcheck Docker

Compose hiện chưa có:

```yaml
healthcheck:
```

Có thể thêm kiểm tra `/health` để Docker và hệ thống giám sát biết API thực sự
sẵn sàng.

## 153. Thêm reverse proxy và HTTPS

Không nên để API production chỉ chạy HTTP trực tiếp trên cổng 8000.

Nên có:

- Nginx/Caddy/ALB.
- HTTPS.
- Domain.
- Giới hạn request.
- Access log.

## 154. Quản lý tăng trưởng artifact

Mỗi lần train tạo:

- Data version mới.
- Model version mới.
- MLflow artifact mới.

Cần chính sách:

- Giữ bao nhiêu phiên bản.
- Backup phiên bản quan trọng.
- Xóa artifact cũ theo lịch.

---

# PHẦN 20: LỘ TRÌNH HỌC VÀ PHÁT TRIỂN

## 155. Bài tập 1: hiện model version trên web

1. Sửa `/model-info`.
2. Đọc `best_model_info.json`.
3. JavaScript gọi `/model-info`.
4. Hiện `model_version`.

Bạn sẽ học:

- Đọc JSON.
- API GET.
- Kết nối frontend và backend.

## 156. Bài tập 2: hiện trạng thái drift

Tạo endpoint:

```text
GET /drift-status
```

Đọc:

```text
monitoring/drift_state.json
```

Hiện:

- Current MAE.
- Baseline MAE.
- Có drift không.
- Candidate đang chờ không.

## 157. Bài tập 3: cho chọn địa điểm

Thêm request:

```json
{
  "date_time": "...",
  "latitude": 44.98,
  "longitude": -93.26
}
```

Nhưng cần nhớ:

> Model hiện học dữ liệu của một khu vực cụ thể. Cho chọn nơi khác không tự làm
> model chính xác ở nơi đó.

Muốn dự đoán nhiều nơi cần dữ liệu training của nhiều nơi hoặc model riêng.

## 158. Bài tập 4: test preprocess

Ví dụ:

```python
def test_weekend_is_one():
    ...
```

Kiểm tra Chủ nhật tạo:

```text
is_weekend = 1
```

## 159. Bài tập 5: test API

Dùng FastAPI TestClient để kiểm tra:

- `/health` trả 200.
- Thiếu `date_time` trả lỗi.
- `/predict` trả trường `prediction`.
- Open-Meteo lỗi thì fallback hoạt động.

---

# PHẦN 21: CÁCH ĐỌC CODE KHÔNG BỊ RỐI

## 160. Đừng đọc từng dòng trước

Hãy đọc theo ba câu hỏi:

1. Dữ liệu đi vào là gì?
2. Hàm làm thay đổi gì?
3. Kết quả đi ra là gì?

Ví dụ:

```python
def evaluate_model(model, dataframe):
    features = align_features(model, dataframe)
    predictions = model.predict(features)
    actual = dataframe["traffic_volume"]
    mae = mean_absolute_error(actual, predictions)
    return float(mae), predictions
```

Đọc thành lời:

```text
Đầu vào:
  model và bảng dữ liệu

Xử lý:
  lấy feature
  dự đoán
  lấy đáp án thật
  tính MAE

Đầu ra:
  MAE và danh sách dự đoán
```

## 161. Theo dấu một lần bấm Predict

```text
index.html
-> fetch("/predict")
-> app.py predict()
-> get_weather_features()
-> predict_single()
-> prepare_input()
-> preprocess()
-> joblib.load(best_model.pkl)
-> model.predict()
-> JSON
-> index.html hiển thị
```

## 162. Theo dấu một lần drift

```text
retrain_job.py
-> load_state()
-> lấy current_window
-> evaluate_pending_candidate()
-> get_historical_mae_baseline()
-> detect_drift_by_mae()
-> drift=True
-> build_rolling_train_window()
-> train_candidate()
-> run_pipeline()
-> lưu candidate_model.pkl
-> tháng sau mới so Candidate với Champion
```

## 163. Điều quan trọng nhất cần nhớ

Toàn bộ dự án có ba vòng chính:

```text
Vòng 1: HỌC
CSV -> preprocess -> train -> model

Vòng 2: PHỤC VỤ
Web -> API -> model -> prediction

Vòng 3: THÍCH NGHI
Dữ liệu mới -> MAE -> drift -> Candidate -> promotion
```

Nếu hiểu được ba vòng này, bạn đã hiểu xương sống của dự án.

Bạn không cần thuộc mọi hàm ngay lập tức. Hãy chạy từng phần, xem log và quay
lại đối chiếu đúng đoạn code tương ứng.
