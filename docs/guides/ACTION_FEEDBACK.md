# Thông báo lỗi và đăng nhập trên giao diện

Bản cập nhật chỉ đổi frontend. Mục tiêu: thao tác bị từ chối hoặc dịch vụ phụ lỗi
phải được giải thích trên màn hình, không im lặng hoặc đẩy người dùng ra Login.

## Thao tác bị từ chối

- **Activate model**, **bật/tắt vùng**, **Toggle role / Enable / Disable user**:
  nếu API từ chối (ví dụ model chỉ để benchmark, admin cuối cùng), lý do hiện trong
  khung cảnh báo của chính màn hình đó. Công tắc vùng trở về trạng thái thật.
  Nếu thay đổi đã lưu nhưng tải lại danh sách lỗi thì báo riêng điều đó.
- **Lỗi kiểm tra dữ liệu (422)**: `apiErrors.js` đổi danh sách lỗi của FastAPI thành
  câu đọc được, ví dụ `New password: String should have at least 8 characters`.
  Trang lỗi cổng (HTML, 502/503) không còn làm vỡ bộ đọc JSON; hiện câu chung kèm mã HTTP.

## Đăng nhập và khôi phục phiên

- Chỉ lỗi xác thực (401/403 ở `/auth/me`) mới xóa token và quay về Login, kèm lý do.
- Lỗi tạm thời khi lấy phiên (mạng, 5xx) giữ token và hiện
  `Could not sign you in right now: ...` ở màn hình Login.
- Đăng nhập thành công nhưng **danh sách vùng** lỗi: giữ phiên, hiện khung
  `Could not load the region list` có nút **Retry**.

## Train lại

Sau khi một lượt train hoàn tất, dòng dataset hiện `Model ready` cùng nút
**Train again** (mở lại Select models, tạo request mới). Không cần F5.

## Kiểm tra

`frontend/tests/apiErrors.test.js` (`npm test`) và
`scripts/maintenance/check_action_errors_ui.py` (trình duyệt, API giả). Đây là kiểm
hành vi giao diện, chưa phải nghiệm thu production.
