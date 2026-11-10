# Cập nhật code qua PR

Mục tiêu: dev nhận phản hồi sớm, cập nhật qua cùng một workflow và giảm các
lệnh triển khai thủ công. Component registry/Add/Remove workload được hoãn;
hướng dẫn này áp dụng cho các thành phần hiện có.

## Cách làm hằng ngày

1. Tạo nhánh từ `master`, sửa code và push nhánh đó.
2. Mở PR vào `master`. Check bắt buộc là `acceptance` của **01 Application
   Delivery**; không yêu cầu job CD hoặc legacy luôn chạy trên PR.
3. Sửa lỗi được CI chỉ ra. Khi check đạt, merge PR; chính sách nhóm có thể
   yêu cầu giải quyết các cuộc trao đổi trước khi merge.
4. Push sau merge chạy kiểm phát hành và chọn image/rollout theo phạm vi
   thay đổi. Production Manual vẫn cần người có quyền duyệt environment.

PR không publish image hoặc triển khai production. Chính sách nhánh chỉ bật
sau khi phiên bản PR gate này đã được kiểm chứng trên GitHub; tài liệu source
không chứng minh settings đã được áp dụng.

| Thay đổi trên PR | Việc cần chạy |
| --- | --- |
| README hoặc tài liệu Markdown trong docs/ | Scope và acceptance nhẹ |
| Frontend | Build và xác minh artifact frontend |
| Backend, worker, DAG, dependency hoặc test backend | Regression backend và dựng/kiểm gói source |
| Workflow hoặc đầu vào chưa phân loại được | Cả backend và frontend |
| Không xác minh được diff PR | Cả backend và frontend, không chọn đường tắt |

Scope PR lấy từ diff của chính PR, không phụ thuộc trạng thái CD hoặc secret
của repository. Diff gồm file đã thêm/xóa; PR sửa nhiều phần chạy hợp của các
kiểm tra liên quan. `docs/index.html` được phục vụ bởi API nên vẫn kiểm backend.
Mọi PR vào nhánh chính đều có workflow; không lọc paths ở trigger PR khiến
required check bị kẹt Pending.

Đây là phản hồi trước merge. Push phát hành vẫn tính thay đổi tích lũy từ
backend đã Verify, kiểm current/legacy khi cần và dùng gói đã kiểm để CD.
Việc một job được bỏ qua đúng scope không tự chứng minh image đã được reuse
hoặc workload được Keep; xem quyết định image và receipt của release.

## Điểm khôi phục trước thay đổi

Mốc đã nghiệm thu: `e073065`, CI40/run42803416548/attempt1. Bản sao local
được chuẩn bị dưới `results/cicd/ci-simplification/recovery-ci40/`, ngoài Git:

- Git bundle và bare clone dùng để kiểm source có thể đọc lại.
- Gói release40 đã kiểm digest GitHub, CRC, manifest và image binding.
- Cấu hình GitHub liên quan không chứa token/credential.
- OCI layout cho image; chỉ gọi đầy đủ khi `image-inventory.json` ghi
  `complete=true` và các blob đã được kiểm SHA256.

Đây là điểm khôi phục source/image/cấu hình CI/CD, không phải backup DB,
MinIO hoặc toàn cụm. Bản copy cùng ổ đĩa không bảo vệ khi ổ đĩa hỏng; giữ bản
sao trên thiết bị/kho độc lập phù hợp. Không đưa credential hoặc thư mục
private vào Git. Git bundle hoặc receipt không tự khôi phục secrets/settings.

Khôi phục source qua một thay đổi được kiểm và phát hành mới. Không ghi đè
CD transaction state bằng snapshot cũ hoặc replay một CI cũ ngẫu nhiên.
Thay schema/storage/data cần kế hoạch backup/restore riêng trước khi làm.

## Runner Windows

`runner.ps1` bổ sung startup theo **tài khoản Windows hiện tại khi đăng nhập**
và supervisor kiểm listener. Nó dùng registration và `StartRunner` hiện có,
giữ CA/Git longpaths, không đăng ký lại runner hoặc thay kubeconfig.

```powershell
./scripts/cicd/runner.ps1 -Action Check
./scripts/cicd/runner.ps1 -Action Install
```

Install tạo một entry riêng trong HKCU Run và chạy supervisor ẩn. Listener
đã chạy được giữ nguyên; nếu listener mất nhưng worker còn chạy, supervisor
chờ worker, không mở listener thay thế. Mutex ngăn supervisor trùng trong
cùng session. Lỗi khởi động được retry có backoff; status local không chứa
credential hoặc log thô.

Trạng thái `listener_present` chỉ chứng minh có process. Nếu Actions vẫn
Waiting for runner, kiểm cả status online/busy và labels trên GitHub, cùng
kết nối Tailscale/cluster. Không kết luận online chỉ từ process local.

```powershell
./scripts/cicd/runner.ps1 -Action Uninstall
```

Uninstall bỏ autostart và yêu cầu dừng supervisor; giữ listener và job đang
chạy. Không stop/kill runner trong một transaction CD. Không dùng thao tác
khởi động lại runner như cách retry Apply.

Đây không phải service chạy trước đăng nhập. Máy tắt, sleep hoặc tài khoản
logoff vẫn có thể làm runner offline. Cài service chính thức cần quyền và
account phù hợp, kiểm CA/Tailscale/kubeconfig, có thể cần reconfigure theo
[GitHub Docs](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application).
HKCU Run chạy sau logon theo [Microsoft](https://learn.microsoft.com/en-us/windows/win32/setupapi/run-and-runonce-registry-keys).
Chưa coi việc cài startup là bằng chứng đã thử reboot/logoff/recovery thực tế.

## Bảo vệ nhánh phù hợp nhóm

Đề xuất `master` nhận thay đổi qua PR, check `acceptance` của GitHub Actions,
chặn force push/xóa nhánh và áp dụng cả admin. Có thể không bắt buộc thêm
người duyệt để nhóm nhỏ tự merge khi CI đạt; chính sách review tăng theo nhu
cầu nhóm. Giữ check cập nhật với nhánh đích và giải quyết conversation trước
merge. Không bắt production CD phải thành công trước merge vì CD chạy sau
push nhánh chính.

Trước khi bật, kiểm PR docs/frontend/backend và kiểm lỗi/skip bắt buộc; giữ
bản cấu hình cũ để phục hồi nếu chính sách gây chặn ngoài dự kiến. Không
tự thay quyền environment hoặc bật Auto khi sửa chính sách PR.
