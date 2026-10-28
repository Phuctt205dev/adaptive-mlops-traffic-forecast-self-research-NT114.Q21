# Bản vá kết nối Cloud Training sau lượt kiểm thử 28/10/2026

Local đã được người dùng kiểm thành công cả ba model cây và hai model neural.
Cloud lỗi trước khi gửi notebook. DB xác nhận lượt cloud mới nhất đã dừng/dọn
xong, không có model được train trên Kaggle từ lượt đó.

## Đã tìm và kiểm tra

Adapter cũ đặt timeout sau xác thực; SDK lại truyền `timeout=None`, khiến
`setdefault` không giới hạn được thời gian mạng. Mỗi RPC còn xác thực thêm
một lần dù import Kaggle đã xác thực. Chẩn đoán từ worker tái hiện được
ConnectTimeout khi gọi API, trong khi token vẫn đúng và HTTPS truy cập được.
Log lượt cũ chỉ giữ cloud_unavailable nên không truy ngược chính xác từng
request lỗi của lượt đó; bản vá xử lý các lỗi đã tái hiện và giữ chẩn đoán
chuẩn hóa cho lần sau.

Bản vá đặt timeout trước cả import/auth, xử lý timeout=None và dùng phiên API
đã xác thực. Chỉ thử lại kết nối TCP chưa gửi được request, tối đa ba lần;
response timeout và phản hồi bị mất vẫn đi qua journal/reconcile, không tự
gửi lại một lượt fit. Controller lưu operation/category/http_status thay cho
exception thô. Nội dung token, headers và lỗi thô không vào monitor.

Hai bài private synthetic từ **node02 và node03** đã upload, đọc metadata
private/version và xóa xác nhận thành công. Không notebook/fit, không DB/S3
nghiệp vụ. Gói mới đã khởi tạo được trên tree/recurrent/API trong process riêng.

## Áp dụng bằng PowerShell

Chạy từ `main_project`, từng lệnh một:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\cloud_training\network-hotfix.ps1 -Action Check
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\cloud_training\network-hotfix.ps1 -Action Apply
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\cloud_training\network-hotfix.ps1 -Action Verify
```

Check chỉ đọc/server dry-run. Apply do người dùng chạy: kiểm không còn lượt
đang chạy/chờ dọn, tạm pause DAG, cập nhật volume mã trên **hai worker và API**,
giữ Cloud Training bật, kiểm mọi pod rồi trả DAG về trạng thái pause ban đầu.
Image, ML environment, thuật toán/split, dữ liệu, DAG, migration và frontend
không đổi. Không cần đăng nhập Kaggle lại, tạo Secret mới hoặc publish frontend.

Bundle mới: `aa3014b55aea9c9cc6bf1fbd6ec5677ac3b43ef23bdd136bcebf223ab75d8082`.
Gói: `results/cloud-training/cloud-network-fix`.
Sau bản vá, dùng **network-hotfix.ps1 -Action Verify** để kiểm bundle mới;
Verify của bộ rollout cũ vẫn đối chiếu bundle cũ.

Nếu Apply lỗi giữa chừng, giữ `hotfix-journal.json`; DAG có thể còn pause.
Không chạy lại Apply khi trạng thái chưa rõ. Có thể dùng action Rollback khi
không còn lượt training đang chạy/chờ dừng; nó chỉ khôi phục volume mã của
trước bản vá, không xóa DB/model/dataset. Không chạy lại Stage/Enable của gói cũ.

## Kiểm thử lại

1. Với lượt lỗi cũ đã chuyển Failed và dọn xong, admin có thể Dismiss thông báo.
2. Chọn **một Random Forest**, bật Cloud Training và gửi **một lượt mới**.
3. Theo dõi Training Monitor và Airflow log: Kaggle → chuẩn bị → fold/model →
   lưu kết quả → monitor trống. Sau khi cây đạt mới thử LSTM/GRU.
4. Nếu vẫn lỗi, lấy run UUID và gọi:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\cloud_training\rollout.ps1 -Action InspectRun -RunId 'UUID-CUA-LUOT-MOI'
```

Output có `branches.*.failure` với operation/category/http_status đã chuẩn hóa.
Gửi output đó cùng UUID; không cần token hoặc log SDK thô. Lệnh InspectRun chỉ
đọc, vẫn dùng được sau hotfix. Chưa coi cloud đạt nghiệm thu toàn luồng chỉ từ
bài kiểm upload; cần lượt model sau áp dụng thành công.
