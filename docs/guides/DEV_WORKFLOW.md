# Cập nhật code qua PR

Mục tiêu: dev nhận phản hồi sớm, cập nhật qua cùng một workflow và giảm các
lệnh triển khai thủ công. Component registry/Add/Remove workload được hoãn;
hướng dẫn này áp dụng cho các thành phần hiện có.

## Luồng cập nhật thường ngày

Sửa code → push/PR → kiểm tra liên quan → merge → build/reuse image → tự cập
nhật đúng thành phần trên RKE2 → kiểm hoạt động và ghi kết quả. Chế độ backend
Manual/Auto đã lưu tiếp tục quyết định có chờ bạn duyệt hay không.

Source ứng dụng, các manifest API/tree/neural hiện có, DAG cloud và dependency
role dùng regression, kiểm gói/image và kiểm thực tế khi triển khai. Các thay
đổi này không dựng cụm Kubernetes thử lại trước mỗi lần cập nhật. Thành phần
không đổi được Keep theo desired state; mã dùng chung có thể ảnh hưởng nhiều
image. Không coi việc một job được skip là bằng chứng pod đã được giữ nguyên.

Hai script kiểm giao diện đã biết (`check_feature_importance_ui.py` và
`check_training_monitor_ui.py`) được nhận diện là công cụ kiểm, không kéo theo
release backend hoặc migration legacy. Script khác/chưa nhận diện chạy các
kiểm nhanh phù hợp. Frontend chạy toàn bộ test Node trong frontend/tests trước build.

**Mọi lượt push phát hành đều không dựng cụm thử**, kể cả sửa engine, workflow,
cache/lifecycle, hợp đồng runtime hoặc schema. Thiếu lịch sử đã Verify hoặc
đầu vào chưa nhận diện thì chạy tất cả kiểm nhanh: regression, frontend,
package và bốn image checks. Các bộ current-runtime/legacy chỉ chạy khi chủ
động chọn full hoặc tới lịch tuần; không tự lặp full sau merge.

Đây là phạm vi kiểm do người dùng lựa chọn: các ca rollout/rollback trên kind
không được chạy trước từng release. Preflight, guard, drain/flush, Verify và
rollback khi triển khai thật vẫn bắt buộc. Kiểm full không gọi Kaggle thật.

**Run workflow** của Application Delivery có lựa chọn `validation`:

- `daily` (mặc định): kiểm theo thay đổi tích lũy từ backend đã Verify.
- `full`: chạy đủ current/legacy và frontend, phù hợp khi muốn kiểm sâu.

Hai lựa chọn manual chỉ kiểm tra, không triển khai RKE2 hoặc Pages. Lịch tuần
vẫn full; phát hành thật chạy sau push nhánh chính và luôn đi đường nhanh,
kể cả chính bản sửa policy này. Chưa có số đo hosted để hứa một thời gian
cố định; thời gian build, CD, chờ duyệt và Kaggle được tách riêng.

## Nếu cập nhật lỗi

| Tình huống | Cách xử lý |
| --- | --- |
| Regression/build/image/preflight lỗi trước Apply | Dừng, giữ bản đang phục vụ; sửa rồi tạo lượt hợp lệ mới. |
| Rollout/Verify/flush lỗi trong transaction | Engine thu chứng cứ và rollback khi ownership/spec còn phù hợp; workflow vẫn failed dù phục hồi thành công. |
| Runner mất kết nối hoặc rollback chưa xác minh | Giữ chốt/owner để kiểm phục hồi, không Apply chồng hoặc sửa state cho qua. |
| Triển khai đã completed nhưng thử chức năng mới thấy sai | Phát hành phục hồi từ phiên bản tốt, kiểm tương thích dữ liệu; không replay journal rollback của release completed. |

Health xanh không chứng minh mọi chức năng đúng. Rollback ứng dụng không
hoàn tác dataset, model đã Activate hoặc schema/data. Image/build/binding,
training guard, drain/flush, Verify và cơ chế rollback hiện có được giữ.

Bản CI/CD trước thay đổi được lưu tại
`D:\You Are Here\HK_VII\KLTN\ideas\2026-11-11-cicd-integrated-0f502dd`;
đọc RESTORE.md và kiểm VERIFY.ps1 trước khi phục hồi, không chép đè CDstate.

Thiết kế theo hướng phản hồi CI trong vài phút và dùng script triển khai,
gói đã build cùng smoke test, theo [DORA Continuous Integration](https://dora.dev/capabilities/continuous-integration/)
và [Deployment Automation](https://dora.dev/capabilities/deployment-automation/).
Đây là nguồn thiết kế, không phải chứng nhận hoặc benchmark đã đạt.

## Cách làm hằng ngày

1. Tạo nhánh từ `master`, sửa code và push nhánh đó.
2. Mở PR vào `master`. Check bắt buộc là `acceptance` của **01 Application
   Delivery**; không yêu cầu job CD hoặc legacy luôn chạy trên PR.
3. Sửa lỗi được CI chỉ ra. Khi check đạt, merge PR; chính sách nhóm có thể
   yêu cầu giải quyết các cuộc trao đổi trước khi merge.
4. Push sau merge chạy kiểm phát hành và chọn image/rollout theo phạm vi
   thay đổi. Production Manual vẫn cần người có quyền duyệt environment.

`frontend-build` thành công ghi nhận việc build/upload đã xong. Job
`acceptance` tiếp tục tải và xác minh gói trên runner riêng; PR chỉ đạt khi
gói tải lại đúng source, cấu hình, attempt và toàn bộ file/hash.

PR không publish image hoặc triển khai production. Branch protection đã được
áp dụng tại mốc CI46/47; khi đổi repository hoặc khôi phục, kiểm settings thật
trên GitHub, không suy từ tài liệu source rằng settings tự được áp dụng.

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
