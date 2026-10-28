# Kết nối Kaggle và kiểm tra khả năng training

Ngày chuẩn bị: 28/10/2026. Tài khoản dự kiến: **truongphucdev**.

**Tiến độ mới:** đã kiểm supervisor thực tế, hủy CPU/GPU, năm loại model,
SHAP và nạp CPU; đã chuẩn bị gói rollout. Xem
[hướng dẫn triển khai hiện hành](KAGGLE_TRAINING_ROLLOUT.md).
Các mục PoC dưới đây giữ quy trình và giới hạn tại thời điểm kiểm ban đầu;
không cần login/chạy lại bài thử đã đạt.

Đây là bước 1–2 của kế hoạch. Công cụ đã được chuẩn bị để đăng nhập, gửi
một notebook riêng tư có dữ liệu giả, xem trạng thái/log và kiểm file nhận
về. Chưa thay worker, Airflow, API hoặc giao diện đang chạy. Auto retrain
và tự chuyển cloud/local không được thực hiện trong bước này.

## 1. Việc cần làm đầu tiên

Mở PowerShell tại thư mục `main_project`, chạy:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\cloud_training\setup-kaggle.ps1 -Action Setup
```

Script dùng môi trường riêng tại `results/kaggle-poc/venv`, không cài
TensorFlow/SDK vào môi trường ứng dụng chính. Phiên bản ghim:
`kaggle==2.2.5`, `kagglesdk==0.1.38`.

Kết quả dự kiến khi chưa nhập token:

```json
{"credential_configured":false,"online_checked":false,"production_ready":false}
```

Các field khác có thể xuất hiện. Bước Setup chỉ cài và kiểm công cụ,
không upload notebook, chạy GPU hoặc tạo dataset trên Kaggle.

## 2. Tạo token trên Kaggle

1. Đăng nhập đúng tài khoản `truongphucdev`.
2. Trong menu đã chụp, chọn **Your API tokens**.
3. Tạo API token mới bằng chức năng tạo token của trang. Có thể đặt tên
   `traffic-training-poc`. Chúng ta dùng token mới, không cần đăng ký billing.
4. Chạy lệnh dưới đây trên máy của bạn rồi dán token vào **prompt ẩn trong
   PowerShell**, tuyệt đối không dán vào chat, notebook, mã nguồn hoặc Git.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\cloud_training\setup-kaggle.ps1 -Action Login -Username truongphucdev
```

Prompt không hiện ký tự khi gõ/dán là bình thường. Công cụ chỉ lưu token
sau khi Kaggle xác nhận đúng username. File ở
`%USERPROFILE%\.kaggle\access_token`, ngoài repository; trên Windows script
giới hạn quyền đọc file cho tài khoản Windows hiện tại. Nếu file đã tồn
tại, công cụ không ghi đè; dùng `-Action Doctor` để kiểm credential sẵn có.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\cloud_training\setup-kaggle.ps1 -Action Doctor -Username truongphucdev
```

Kết quả cần có `online_checked: true`, `authenticated: true` và quota.
Chỉ gửi bản tóm tắt đó cho AI; không gửi file token. Nếu báo lỗi, dừng ở
bước đó và gửi thông báo đã được công cụ che chi tiết credential.

Nếu trang Kaggle yêu cầu hoàn tất điều kiện tài khoản hoặc xác minh trước
khi cấp accelerator, thực hiện theo giao diện Kaggle. Quota xuất hiện trên
menu chưa tự chứng minh GPU đã được cấp khi chạy.

## 3. Chuẩn bị bài thử riêng tư — chưa chạy

Sau khi đăng nhập đạt, dùng Python riêng của PoC:

```powershell
& .\results\kaggle-poc\venv\Scripts\python.exe .\scripts\cloud_training\kaggle_poc.py prepare --username truongphucdev --accelerator cpu
```

Lệnh trả `job_directory` và notebook reference dạng
`truongphucdev/traffic-poc-...`. Sao chép đường dẫn đó vào biến:

```powershell
$probeJob = 'DUONG_DAN_job_directory_VUA_NHAN'
```

Mỗi bài thử có source/hash, notebook riêng tư và mã job riêng. Không có
token, dữ liệu dự án, database dump hoặc credential nội bộ trong gói.
Không sửa file đã chuẩn bị; nếu sửa, checker sẽ yêu cầu tạo job mới.

## 4. Gửi và theo dõi bài thử

Lệnh `submit` bên dưới **tạo notebook và chạy thật trên Kaggle**. Chỉ chạy
khi đã thống nhất bài thử. Mặc định CPU giúp kiểm kết nối trước khi dùng
quota GPU. Mỗi phiên PoC được gửi với timeout tối đa 240 giây; thời gian
xếp hàng và quota vẫn phụ thuộc Kaggle.

```powershell
& .\results\kaggle-poc\venv\Scripts\python.exe .\scripts\cloud_training\kaggle_poc.py submit --job $probeJob
& .\results\kaggle-poc\venv\Scripts\python.exe .\scripts\cloud_training\kaggle_poc.py status --job $probeJob
& .\results\kaggle-poc\venv\Scripts\python.exe .\scripts\cloud_training\kaggle_poc.py logs --job $probeJob --seconds 30
```

Lệnh logs chỉ nhận sự kiện do bài thử phát, có job ID/epoch và giới hạn
thời gian. Kết thúc hoặc Ctrl+C lệnh xem log chỉ dừng bộ đọc trên máy;
**không được hiểu là đã hủy job Kaggle**. Nếu không đọc được log, công cụ
báo mất giám sát, không tự kết luận training failed.

Nếu gửi bị timeout, không bấm submit lần nữa: trạng thái
`submission_pending` chặn gửi trùng. Cần đối chiếu notebook trên Kaggle
trước khi quyết định bước phục hồi; không xóa job.json để vượt kiểm tra.

Sau đối chiếu API thật, submit GET lại metadata/source/privacy để xác minh
thay vì chỉ dựa định dạng SaveKernel.ref. Nếu request mất phản hồi, dùng
`reconcile --job $probeJob` để kiểm chỉ đọc; source, quyền riêng tư,
tài nguyên và version phải khớp. Không gửi version mới để vượt lỗi xác nhận.

Khi CPU smoke đạt, tạo **job mới** bằng cùng lệnh prepare với
`--accelerator gpu`, sau đó theo dõi theo cùng quy trình. Notebook kiểm
TensorFlow thật có GPU; không nhận trường hợp yêu cầu GPU nhưng chạy CPU
là bài thử GPU đạt. Đây là LSTM cực nhỏ trên số liệu giả, không thay
thuật toán/model nghiệp vụ.

## 5. Nhận và kiểm kết quả

Chỉ khi status là `COMPLETE`:

```powershell
& .\results\kaggle-poc\venv\Scripts\python.exe .\scripts\cloud_training\kaggle_poc.py fetch --job $probeJob
& .\results\kaggle-poc\venv\Scripts\python.exe .\scripts\cloud_training\kaggle_poc.py verify --job $probeJob
```

Chỉ nhận năm file được phép; kiểm job ID, manifest, size/SHA256,
preprocessing và bằng chứng GPU. `output_verified: true` mới chỉ chứng
minh file đúng, chưa chứng minh model load được trên serving CPU.

Lệnh `reload-cpu` chỉ chạy trong môi trường test CPU đã có TensorFlow phù
hợp, sau kiểm file. Không tự cài TensorFlow nặng lên máy hoặc chạy lệnh
trên API serving. Công cụ so bốn mẫu synthetic bằng dung sai định trước:

```powershell
python .\scripts\cloud_training\kaggle_poc.py reload-cpu --job $probeJob
```

Mô hình này không được đăng ký registry/MLflow trung tâm hoặc kích hoạt.

## 6. Điều tra PoC ban đầu: hủy native theo session

Đối chiếu SDK ghim đã tải cho thấy:

- SDK có log stream theo owner/notebook/version; cần xác nhận live bằng
  bài thử trước khi nối vào monitor.
- `SaveKernel` trả notebook `kernel_id` và `version_number`.
- `CancelKernelSession` cần `kernel_session_id`; notebook ID không phải
  session ID. Status cũng chưa trả mapping này trong schema SDK đang dùng.

Vì vậy, công cụ **không đoán session ID**, không xóa notebook để giả hủy,
không cho bật cloud production khi chưa chứng minh mapping và xác nhận
hủy thật. Lệnh cancel đã có adapter nhưng bị chặn nếu không có mapping
đã được kiểm chứng; không chỉnh job.json bằng tay để vượt cửa kiểm tra.

PoC có `prepare --purpose cancel` để tạo target riêng chờ hủy trong thời
gian giới hạn, không tạo model artifact. Không gửi target này trước khi
đã chốt cách điều khiển/cancel. Cancel API trả thành công chỉ là đã gửi
yêu cầu; phải kiểm trạng thái `CANCEL_ACKNOWLEDGED` của đúng version.

Nếu API hiện tại không giải được mapping một cách chính thức, báo giới
hạn và kiểm phương án khác trước khi triển khai cloud. Không giảm yêu cầu
“hủy tiến trình thật” thành “ngừng nhận kết quả”.

## 7. Bằng chứng để đi tiếp

Đi tiếp sang worker/API/UI sau khi có: xác thực và quyền GPU, artifact tải
về/load CPU đạt, progress thật, hủy đúng session có xác nhận, và cơ chế
dừng local được kiểm bằng bài thử cô lập. Các test giả lập không thay
những bằng chứng này. Kết quả hiện tại luôn ghi `production_ready: false`
để không nhầm chuẩn bị PoC với hoàn thành triển khai.

## 8. Kết quả live đã đạt ngày 28/10

Đã đăng nhập và chạy CPU/GPU smoke bằng dữ liệu giả trên truongphucdev.
GPU report thấy2thiết bị, TensorFlow2.20.0. ArtifactGPU được nạp bằng
CPU/TensorFlow2.21.0 trong tiến trình kiểm tạm của worker đang rảnh:4mẫu
khớp, sai số lớn nhất4.47e-8; khôngfit/ghiDB/S3/registry/thayDeployment.
Một bàiCPU xác nhận nhận epoch/loss trong lúc version cònRUNNING, không
chỉ đọc log sau hoàn tất. Chi tiết và proof trong hand-off.

Hủy native qua session API vẫn thiếu mapping. Đã kiểm được hướng **hủy
hợp tác** chỉ dùng API Kaggle công khai: notebook đọc source riêng của nó
bằng credential Kaggle cấp; controller Quick Save markerhủy với
`no_run=True`; notebook dừng/wait tiến trình con đang fit, phátcancelled
rồi kết thúc. BàiCPU đã có child_running trước signal và xác nhận dừng
sau signal. Chưa phải triển khai đầy đủ cascade nhiều nhánh hoặc GPU;
vẫn phải nối điều khiển và kiểm thử tích hợp.

```powershell
# Chỉ dùng job riêng có purpose cooperative-cancel, không dùng smoke job.
& .\results\kaggle-poc\venv\Scripts\python.exe .\scripts\cloud_training\kaggle_poc.py cooperative-cancel --job $probeJob
& .\results\kaggle-poc\venv\Scripts\python.exe .\scripts\cloud_training\kaggle_poc.py logs --job $probeJob --seconds 30
& .\results\kaggle-poc\venv\Scripts\python.exe .\scripts\cloud_training\kaggle_poc.py confirm-cancel --job $probeJob
```

Signal đã công bố chưa có nghĩa đã dừng. Phải có quan sát childđang chạy,
eventcancelled đúngjob và trạng thái kết thúc đúngversion. Provider có
thể báoCOMPLETE vì chương trình giám sát thoát bình thường sau hủy;
ứng dụng phải hiểu trạng thái nghiệp vụ làcanceled, không coi đây là model
train thành công. Mất mạng không được tự suy ra đã hủy.

SDK có thể tải thêm slug.log; công cụ chỉ chuyển log tên đã biết vào
provider.log ngoài thư mục model, không nới manifest để nhận file lạ.
Không gửi provider.log thô vì có thể chứa chi tiết môi trường.

Nguồn chính thức đã đối chiếu:

- [Kaggle CLI/API](https://github.com/Kaggle/kaggle-cli)
- [CLI kernels](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md)
- [Vấn đề session identity/cancel](https://github.com/Kaggle/kaggle-cli/issues/1169)
- [Kaggle API authentication](https://www.kaggle.com/docs/api)

## 9. Source và gói triển khai hiện hành, chưa bật cloud

Login/Doctor đạt chỉ xác nhận credential và quota tại thời điểm kiểm tra.
`production_ready:false` vẫn đúng trong công cụ PoC sau khi smoke test đạt.
Không cần nhập lại token hoặc gửi token qua chat.

Source hiện đã có Cloud Training trong modal Train/Retrain, trang admin
Training Monitor và API/state/migration bổ sung. Hai cờ
`training_control_enabled` và `cloud_training_enabled` mặc định false để
không dùng luồng mới khi worker/DAG/deployment chưa được tích hợp và kiểm đủ.
Không tự bật cờ hoặc chạy migration như bước tiếp theo của Login.

Gói thực thi mang source ML theo danh sách import, snapshot CSV và configuration
có hash, không nén cả repo. Bộ đọc output kiểm manifest/kích thước/SHA/context
và đường dẫn. Supervisor mới đã thử live: tiến độ, gia hạn Quick Save, hủy
CPU/GPU, private staging cleanup và model pipeline thực tế. Provider/controller,
Airflow polling, central tracking và cleanup đã nối trong source; kiểm lỗi
liên nhánh/restart có test cô lập. Các bài thử không ghi DB/S3 nghiệp vụ.

Gói review đã qua source startup trên ba vai trò thật và server dry-run, chưa
rollout hoặc chạy toàn luồng ứng dụng sau migration. Người dùng triển khai theo
[hướng dẫn rollout](KAGGLE_TRAINING_ROLLOUT.md). Tiến độ cụ thể nằm trong
[hand-off Kaggle](../../My-worklog/hand-offs/2026-10-28-kaggle-training.md).
