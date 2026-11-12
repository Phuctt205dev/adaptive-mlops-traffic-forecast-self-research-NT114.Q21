# Phục vụ dự báo LSTM/GRU

Dashboard nhiều ngày và Prediction dùng cùng model/preprocessor của phiên bản
được Activate. Model Keras và dữ liệu đã chuẩn bị có cache trong từng process
API; cache không dùng chung giữa hai replica và mất khi pod khởi động lại.

## Giảm chi phí của lần dự báo đầu

Đường serving gọi `prepare_hourly_time_series(..., include_quality_report=False)`.
Các hàng theo giờ, quy tắc điền dữ liệu và audit giữ nguyên; chỉ bỏ tạo báo cáo
offline và backtest che500 target. Upload/training vẫn mặc định tạo đủ báo cáo.
Nếu không có giờ trùng, bước tổng hợp dùng mean dạng bảng và lấy trực tiếp
category của từng giờ, tránh gọi mode cho hàng chục nghìn nhóm chỉ có một dòng.
Giờ trùng vẫn dùng quy tắc gộp cũ.

Model tải với `compile=False` vì dự báo không cần optimizer/loss/metrics để train.
Cả dự báo một giờ và theo batch dùng `model(inputs, training=False)`; mỗi batch
tối đa256 sequence. Giữ dropout tắt, chiều dài sequence, feature đã chọn,
scaler và ranh giới chỉ lấy lịch sử trước giờ đích. Model Quality dùng cùng
đường batch. Không cần train lại model đang lưu.

Nguồn: [Keras load_model](https://keras.io/api/models/model_saving_apis/model_saving_and_loading/),
[Model.predict và lời gọi trực tiếp cho batch nhỏ](https://keras.io/api/models/model_training_apis/).

## Kiểm chứng và giới hạn

Kiểm tự động ở `tests/test_neural_serving.py`: so sánh với phép gộp cũ,
audit/giờ thiếu/trùng, bỏ report offline, cache theo SHA dataset, load không
compile, batch có giới hạn, một giờ/batch dùng cùng lịch sử và scaler.
Ca TensorFlow thật tạo model LSTM/GRU nhỏ bằng trọng số tự sinh, save/load rồi
so kết quả với `predict`; không train, không gọi Kaggle hoặc production.

Đo local ngày12/11/2026 với43.824 dòng synthetic: bước chuẩn bị cũ30,08s;
bản serving sau sửa0,65s ở mẫu đo đầu và0,78s khi đo bản cuối. CSV của hourly/audit có hash
khớp. Đây là phép đo phần chuẩn bị trên máy local, không phải thời gian HTTP,
TensorFlow cold start, benchmark khóa luận hoặc kết quả đo trên RKE2.

Lần đầu mỗi API vẫn có chi phí import TensorFlow, đọc model và khởi tạo phép
tính. Startup hiện có warm model active, nhưng Activate sau startup chưa
prewarm toàn bộ hai replica. Không tăng số pod, RAM limit hoặc nạp mọi
candidate vào bộ nhớ. Sau phát hành, kiểm thực tế lần đầu/lần lặp với cùng
model, dataset và giờ dự báo; tách thời gian chờ request khỏi tính toán.
