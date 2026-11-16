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
`scripts/drift/check_auto_retrain_ui.py` kiểm bản frontend build với API giả:
đổi vùng, tải lại trang, phản hồi chậm và lỗi lưu; không thay cấu hình hay train thật.
