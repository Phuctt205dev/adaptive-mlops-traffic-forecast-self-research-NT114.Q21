# Dữ liệu luôn đúng vùng đang chọn

Giao diện admin/user tải dataset, model, cửa sổ dự báo và dashboard theo từng
vùng. Khi người dùng đổi vùng lúc phản hồi của vùng trước chưa về, phản hồi đó
không được thay thế nội dung của vùng mới. Bản cập nhật chỉ đổi frontend; không
đổi API, dữ liệu hay cách train.

## Quy tắc

- Danh sách dataset/model được lưu cùng vùng đã tải. Đổi vùng thì danh sách cũ
  biến mất ngay và hiện `Loading datasets…`/`Loading models…` cho tới khi vùng
  mới tải xong. Phản hồi muộn của vùng khác bị bỏ qua (`regionScope.js`).
- Hàm làm mới luôn tải vùng đang chọn **tại thời điểm gọi**, kể cả khi được gọi
  từ một màn hình dựng trước khi đổi vùng.
- Train, Activate chỉ chạy khi dòng được chọn thuộc vùng hiện tại (`region_id`).
  Cửa sổ Select models đang mở sẽ đóng khi đổi vùng. Cửa sổ xóa đã xác nhận giữ
  nguyên đối tượng (ID cố định) để thao tác dở dang vẫn thử lại được.
- Prediction tải lại cửa sổ dự báo khi vùng **hoặc model active** đổi, xóa kết quả
  cũ và bỏ phản hồi của ngữ cảnh trước.
- Dashboard tải lại khi sang giờ mới hoặc khi quay lại tab; không giữ dự báo của
  vùng khác trong lúc tải. Đổi sang vùng chưa có model active thì tắt `Updating...`.
  Trong cùng một giờ không gọi lại; xem chi tiết ngày không bị đóng bởi lần làm mới.

## Kiểm tra

- `frontend/tests/regionScope.test.js` (`npm test`): quy tắc cổng yêu cầu,
  phạm vi danh sách, kiểm `region_id`, mốc giờ.
- `scripts/maintenance/check_region_context_ui.py`: build frontend rồi chạy trình
  duyệt với API giả, trì hoãn phản hồi để tái hiện đổi vùng giữa chừng. Không gọi
  hệ thống thật. Đây là kiểm hành vi giao diện, chưa phải nghiệm thu production.
