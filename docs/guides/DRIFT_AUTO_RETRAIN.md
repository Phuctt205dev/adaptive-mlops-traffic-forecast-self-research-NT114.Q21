# Auto retrain theo vùng

Admin bật/tắt **Auto retrain** tại tab Drift cho vùng đang chọn. Lựa chọn
được lưu ngay trên server; không cần bấm Check now để lưu. Đổi vùng, chuyển
tab hoặc tải lại trang sẽ đọc lại lựa chọn của đúng vùng đó.

Vùng chưa từng lưu lựa chọn mặc định **tắt**, kể cả vùng đã tồn tại trước
bản cập nhật. Checkbox cũ chỉ nằm trong bộ nhớ giao diện, không có cấu hình
bền vững để chuyển đổi; admin cần bật lại những vùng muốn tự động retrain.

- Lịch Airflow vẫn kiểm tra drift ở các vùng đủ điều kiện. Chỉ vùng đã bật
  mới được tự động tạo lượt retrain khi phát hiện drift.
- Tham số `auto_retrain=true` của lịch/request không tự bật một vùng; `false`
  vẫn có thể ngăn retrain cho riêng lần kiểm tra. Công tắc toàn hệ thống,
  kiểm tra training đang chạy và thời gian chờ giữa các lượt vẫn được giữ.
- Retrain thủ công là thao tác riêng; tắt Auto retrain không cấm admin tự train,
  và không hủy lượt train đã được đưa vào hàng chờ hoặc đang chạy.
- Trong lúc tải/lưu lựa chọn, checkbox và Check now tạm khóa. Lưu lỗi sẽ có
  thông báo và giao diện đọc lại trạng thái server để tránh giữ tick chưa được xác nhận.

## Khi nào hệ thống tự retrain (theo thực nghiệm E3)

Mỗi ngày lúc 00:00 (DAG `traffic_region_drift_monitoring`), backend so **7 ngày
gần nhất** với **30 ngày ngay trước `train_end_date`** của model active, như cũ.
Từ bản này, **drift được quyết định bằng chính số xe**:

| Tín hiệu | Dùng để | Ngưỡng |
| --- | --- | --- |
| PSI của `traffic_volume` (hàm `calculate_traffic_drift` trong `src/feature_drift.py`) | **Báo drift và retrain** | `drift_traffic_psi_threshold = 1.0` |
| PSI/JS của 13 cột thời tiết, ngày lễ | Chỉ **hiển thị tham khảo** | như cũ (0,2 / 0,15, ≥ 2 cột) |
| Tuổi dữ liệu của model active (ngày kiểm − `train_end_date`) | **Retrain định kỳ** | `drift_scheduled_retrain_days = 28` (0 = tắt) |

Lý do và số đo ở [E3](../../experiments/E3-drift-retrain.md): tín hiệu thời tiết
báo ≈ 98% kể cả khi chắc chắn không có drift (điểm J = 0); PSI số xe > 1,0 báo
nhầm 5%, phát hiện giảm đột ngột 30% sau 5 ngày; với model hiện tại sai số giảm
đều theo số lần retrain nên cần thêm retrain định kỳ.

Quy tắc tự retrain (chỉ khi vùng bật **Auto retrain** và công tắc toàn hệ thống bật):

1. **PSI số xe > 1,0** → retrain, lý do `traffic_drift`. Bỏ qua (`candidate_pending`)
   nếu trong 28 ngày qua đã có lượt retrain tự động **từ chính model active này**
   — model mới của lượt đó đang chờ Activate, retrain lại chỉ lặp cùng việc.
   Admin bấm Check now kèm force vẫn retrain được.
2. Không có drift, **dữ liệu của model active đã cũ ≥ 28 ngày** và 28 ngày qua chưa
   có lượt retrain tự động nào (không tính lượt lỗi/hủy) → retrain, lý do `scheduled`.
3. Các chặn cũ giữ nguyên: đang có training chạy (`training_already_running`),
   thời gian chờ 72 giờ (`cooldown_active`).

Lượt retrain vẫn có `trigger_source = feature_drift` (các dịch vụ khác nhận ra lượt
retrain theo trường này) và thêm `trigger_reason` = `traffic_drift` hoặc `scheduled`.
Model mới vẫn là **candidate**, admin xem và Activate như trước (`drift_auto_promote`
vẫn tắt). Không đổi schema DB: PSI số xe, tuổi dữ liệu và lý do nằm trong JSON
của `drift_checks` (`summary.traffic_psi`, `summary.weather_drift_detected`,
`summary.model_data_age_days`, `retrain_reason`, `traffic`).

Tab Drift: ô **Traffic change (PSI)** (giá trị / ngưỡng), ô thời tiết ghi
"(reference)", trạng thái **Scheduled retrain** nền xanh dương (không phải cảnh
báo), lý do retrain/bỏ qua bằng lời, lịch sử có cột Traffic PSI và lý do. Lần
kiểm lưu trước bản này vẫn hiển thị như cũ.

## API và lưu trữ

Chỉ admin được GET/PATCH `/api/v1/admin/regions/{region_id}/drift-settings`.
PATCH nhận `{"auto_retrain": true}` hoặc `false`; response trả `region_id`
và `auto_retrain`. Không nhận null, chuỗi boolean hoặc field thừa.

Thiết lập dùng bảng AuditLog hiện có, action `region_drift_settings_updated`,
entity_type `region`, entity_id là UUID vùng. Lần đổi giá trị ghi before/after
và admin thực hiện; đọc bản mới nhất, mặc định false khi chưa có bản ghi.
Khóa hàng Region để tuần tự hóa cập nhật giữa các API, giữ thứ tự sự kiện
khi đồng hồ replica lệch nhẹ. Không cache theo process/localStorage, không
thêm schema hay yêu cầu migration runtime cho bản cập nhật này.

## Kiểm chứng

`tests/test_drift_settings.py` kiểm lưu/đọc qua session khác, cách ly vùng,
quyền admin, đầu vào và chặn dispatch ngoài lựa chọn. Training được giả lập.
`tests/test_drift_traffic_decision.py` kiểm PSI số xe, quyết định retrain theo số xe và theo lịch, `candidate_pending`,
lượt lỗi/hủy không chặn lịch, công tắc vùng và `trigger_reason`. `frontend/tests/driftText.test.js` kiểm chữ hiển thị;
`frontend/tests/check_drift_ui.py` kiểm tab Drift với API giả (lý do retrain, lần kiểm cũ, 1440/390/320 px).
`scripts/drift/check_auto_retrain_ui.py` kiểm bản frontend build với API giả:
đổi vùng, tải lại trang, phản hồi chậm và lỗi lưu; không thay cấu hình hay train thật.
