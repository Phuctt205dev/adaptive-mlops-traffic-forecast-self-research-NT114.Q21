# Cửa sổ admin và ghi nhớ vị trí làm việc

## Xác nhận thao tác

Hộp xác nhận xóa model/vùng dùng giao diện của ứng dụng thay cho window.confirm.
Tiêu đề và đối tượng được chọn hiển thị rõ, có mô tả phạm vi, Cancel và nút
Delete model/Delete region màu đỏ. Focus ban đầu ở Cancel; Enter tại Cancel
không xác nhận xóa. Tab/Shift+Tab ở trong cửa sổ, Esc/X/bấm ngoài để hủy,
đóng xong trả focus về nút mở nếu nút còn tồn tại.

Trong lúc xóa, cửa sổ hiển thị bước backend đã lưu: dọn cache/file, tracking,
file lưu trữ và kiểm tra kết quả. Không dùng phần trăm giả hoặc suy heartbeat
là đã xong. Khóa thao tác lặp trong lúc chờ; sau ba phút chưa xác nhận được,
cho đóng hoặc kiểm tra lại. Đóng cửa sổ không hủy công việc phía server.

Mỗi lần admin xác nhận chỉ gửi một DELETE; việc theo dõi tự động chỉ gửi GET
trạng thái. Nếu mất phản hồi, đối soát journal trước khi kết luận. Lỗi giữa
chừng giữ nút Retry deletion để admin tiếp tục các bước còn thiếu. Backend
giữ khóa chống chạy hai lượt dọn đồng thời và xác nhận lại kết quả cuối.

SessionStorage giữ ID/nhãn đối tượng đang xóa theo tài khoản và loại đối tượng
để F5 trong cùng tab trình duyệt có thể đọc tiếp trạng thái. Không lưu token
vào khóa này, không tự gửi DELETE khi khôi phục trang. Nếu storage bị chặn,
cửa sổ hiện tại vẫn hoạt động nhưng không phục hồi được qua F5.

Xóa model nay dọn cả file riêng của lượt tạo model, tracking artifact và cache;
không xóa dataset hoặc experiment dùng chung. Xem [phạm vi cleanup](REGION_CLEANUP.md).

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
