# Cửa sổ admin và ghi nhớ vị trí làm việc

## Xác nhận thao tác

Hộp xác nhận xóa model/vùng dùng giao diện của ứng dụng thay cho window.confirm.
Tiêu đề và đối tượng được chọn hiển thị rõ, có mô tả phạm vi, Cancel và nút
Delete model/Delete region màu đỏ. Focus ban đầu ở Cancel; Enter tại Cancel
không xác nhận xóa. Tab/Shift+Tab ở trong cửa sổ, Esc/X/bấm ngoài để hủy,
đóng xong trả focus về nút mở nếu nút còn tồn tại.

Trong lúc gửi yêu cầu, khóa thao tác lặp và đóng cửa sổ. Nếu API báo lỗi,
giữ thông báo trong cửa sổ để admin xem và thử lại; không tự báo thành công.
Thay đổi chỉ ở frontend, không đổi phạm vi hay điều kiện xóa của backend.

## Refresh giữ tab

Ghi nhớ trang theo tài khoản tại localStorage `traffic_page_<user UUID>`.
Khi khôi phục phiên đăng nhập, chỉ nhận trang hợp lệ theo quyền hiện tại;
trang đã bỏ, giá trị không hợp lệ hoặc trang admin của tài khoản bị đổi quyền
đều trở về Dashboard. Profile cũng được ghi nhớ. Không đổi cơ chế ghi nhớ vùng.
Nếu trình duyệt chặn lưu vị trí trang, điều hướng vẫn dùng được trong phiên.

## Mở rộng Training Configuration

Tại Advanced có nút Expand mở cửa sổ ở giữa màn hình, rộng tối đa960px,
cuộn phần nội dung khi màn hình thấp/nhỏ. Card chính vẫn bằng chiều cao
card upload ở bố cục hai cột; trường hệ thống quản lý vẫn đỏ và chỉ đọc.

Mở rộng và card dùng chung state training form. Mọi ô sửa được cập nhật ngay
vào form; Esc, Done, X hoặc bấm ngoài giữ giá trị khi đóng/mở lại, chuyển
Basic/Advanced hoặc mở phần chọn model để train. Đây là bản nháp trong trang,
không tự lưu cấu hình training vào server hay bắt đầu train; không bổ sung
lưu bản nháp qua refresh trong thay đổi này.

## Nguồn tham khảo đã đối chiếu cho thay đổi này

- [Atlassian modal examples](https://atlassian.design/components/modal-dialog/examples):
  cửa sổ có header/body/footer, hành động rõ và nút đóng.
- [IBM Carbon modal guidelines](https://www.carbondesignsystem.com/building-blocks/core/components/modal/guidelines):
  phân biệt xác nhận thao tác không thể hoàn tác với cửa sổ chỉnh nội dung.
- [W3C modal dialog pattern](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/):
  focus, bàn phím, tên/mô tả và vùng nền không tương tác khi đang mở.

Kiểm thử: `npm test`, `npm run build` và
`frontend/tests/check_admin_dialogs_ui.py` với API giả lập. Không dùng
thao tác xóa/train hoặc tài khoản production để kiểm giao diện này.
