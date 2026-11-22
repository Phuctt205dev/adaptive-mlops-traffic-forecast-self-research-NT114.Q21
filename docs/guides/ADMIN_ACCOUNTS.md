# Bảo vệ tài khoản quản trị

## Admin cuối cùng

`PATCH /admin/users/{id}` từ chối (HTTP 409, mã `last_admin_required`) mọi thay đổi
làm hệ thống không còn admin đang hoạt động: hạ quyền, Disable, hoặc cả hai. Muốn
hạ hay vô hiệu hóa một admin, trước hết phải có admin hoạt động khác. Admin bị
Disable không được tính là admin còn lại.

Hai admin đổi quyền lẫn nhau cùng lúc được tuần tự hóa bằng khóa hàng (`FOR UPDATE`,
thứ tự cố định theo id), nên không thể cùng vượt qua phép kiểm. Giá trị `null` trong
PATCH được coi là "không gửi", không ghi vào cột bắt buộc.

## Tài khoản bootstrap khi khởi động

Tài khoản cấu hình trong `BOOTSTRAP_ADMIN_EMAIL`:

- chưa tồn tại: được tạo với quyền admin;
- đã là admin đang hoạt động: không đổi;
- đã bị hạ quyền hoặc Disable: **giữ nguyên** nếu vẫn còn admin hoạt động khác,
  nên thay đổi của admin không bị đảo lại sau mỗi lần khởi động;
- khi không còn admin hoạt động nào: được khôi phục (admin + active) để hệ thống
  quản trị được trở lại.

Kiểm tra: `tests/test_user_admin_protection.py`. Không đổi schema DB.
