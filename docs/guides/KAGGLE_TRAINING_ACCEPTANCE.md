# Kiểm thử sau khi triển khai Cloud Training

Chỉ bắt đầu sau `rollout.ps1 -Action Verify -Expect enabled` đạt và frontend
mới đã lên. AI chưa chạy các lượt dưới đây trên DB/MinIO của dự án.

## Chuẩn bị dữ liệu và nơi thử

Đăng nhập admin, tạo/chọn một region dùng riêng để thử, chưa có model active.
Upload `results/cloud-training/review-final/cloud-training-synthetic-5000.csv`.
File có 15 cột đúng hợp đồng, 5.000 giờ từ 01/01 đến 28/07/2026, dữ liệu tự sinh;
validator local đã chấp nhận. Không dùng file này để kết luận chất lượng dự báo.

Chọn cửa sổ train **01/01/2026–15/06/2026** nếu giao diện cho chỉnh ngày.
Nếu dùng cửa sổ mặc định do giao diện tính, ghi ngày thực tế vào phiếu kiểm thử.
Không bấm Activate trong các bài này: nghiệm thu training và lưu candidate.

## Các bài kiểm

| Bài | Thao tác | Kết quả cần thấy |
| --- | --- | --- |
| 1. Local | Tắt Cloud Training, chỉ chọn Random Forest, Train | Monitor báo Local; Airflow chạy graph cũ; model được đăng ký sau đủ receipt; monitor trống khi hoàn tất |
| 2. Cloud cây | Bật Cloud Training, chỉ chọn Random Forest, Train | Monitor/Airflow log báo Kaggle; nhánh LSTM/GRU bỏ qua, không dùng GPU; có tiến độ thật; candidate lưu được; monitor trở về trống |
| 3. Cloud neural | Bật Cloud Training, chọn LSTM và GRU | Hai model neural chạy GPU lần lượt LSTM rồi GRU; cây bỏ qua; fold/epoch xuất hiện; CPU reload đạt trước đăng ký |
| 4. Lỗi | Gửi cấu hình lỗi có kiểm soát dưới đây trên dataset thử | Lượt đỏ, yêu cầu dừng cả hai worker, không tạo ModelVersion từ lượt lỗi; chỉ Dismiss sau xác nhận dừng/dọn |

Ở bài 1–3, mở task log Airflow để đối chiếu backend/model/fold/epoch với
Training Monitor. Bar đếm model đã đánh giá; chờ bước lưu kết quả hoàn tất.
Mỗi lần chỉ gửi một lượt; ghi training run UUID trong View details khi còn
đang chạy, hoặc lấy từ danh sách Training runs sau đó.

## Đối chiếu sau từng lượt bằng lệnh chỉ đọc

Thay UUID bằng training run vừa thử:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\cloud_training\rollout.ps1 -Action InspectRun -RunId 'UUID-CUA-LUOT-TRAIN' -RunState completed
```

Mong đợi `acceptance_check_passed:true`, input dataset còn, có model đăng ký,
không có model active từ lượt thử, mọi nhánh đã dọn staging. Nếu vừa hoàn tất
mà watcher chưa dọn xong, lệnh sẽ trả trạng thái hiện tại và chưa pass; đợi rồi
kiểm lại. Lệnh không chạy/hủy job, ghi DB hoặc xóa output.

Sau bài lỗi, dùng `-RunState failed`. Mong đợi không có model đăng ký của lượt
đó, nhánh đã khởi động đều xác nhận dừng/dọn, input còn nguyên. Các cờ dừng/dọn
đọc từ trạng thái controller đã ghi sau khi kiểm provider/storage, không tự
suy ra job đã dừng chỉ vì không nhận được log.

Proof được lưu ở `results/cloud-training/review-final/run-<UUID>-proof.json`.
Kiểm thêm MLflow qua UI hiện có: lượt thành công có run kết thúc và artifact
của model được chọn; lượt lỗi không giữ model/artifact lỗi. Kết quả của lượt
thành công trước phải vẫn truy cập được.

## Cấu hình lỗi có kiểm soát

Trên trang API docs của backend, dùng tài khoản admin và gọi
`POST /api/v1/datasets/{dataset_id}/train` với dataset thử vừa upload.
Cửa sổ năm 2025 dưới đây cố ý không có trong CSV năm 2026: dữ liệu upload
vẫn hợp lệ, nhưng bước chuẩn bị training sẽ thất bại. Chọn cây + LSTM để kiểm
yêu cầu dừng lan giữa hai worker. Bắt đầu bằng local để kiểm nhanh:

```json
{
  "training_backend": "local",
  "train_start_date": "2025-01-01",
  "train_end_date": "2025-02-01",
  "selected_models": ["random_forest_lag", "lstm"],
  "cv_splits": 3,
  "recurrent_epochs": 1,
  "recurrent_sequence_length": 24,
  "recurrent_batch_size": 64,
  "shap": {"enabled": false}
}
```

Lấy UUID từ `training_run.id` trong response. Nếu request bị từ chối trước
khi tạo run thì bài này chưa kiểm được hủy lan nhánh; gửi mã lỗi đã được làm
sạch để đối chiếu, không ghi nhận pass. Sau bài local đạt, có thể chạy lại với
`training_backend:"kaggle"` để kiểm lỗi/cleanup trên cloud, có tiêu hao quota
khi notebook được cấp GPU. Nếu một nhánh chưa kịp bắt đầu trước lỗi thì trạng
thái cancelled/không khởi động là đúng; bài hủy GPU khi đang fit đã được kiểm
riêng ở PoC, không suy ra từ một nhánh chưa bắt đầu.

## Ghi kết quả

| Bài | Training run UUID | Backend | UI/Airflow khớp | InspectRun pass | MLflow/kết quả cũ còn | Ghi chú |
| --- | --- | --- | --- | --- | --- | --- |
| Local RF | | | | | | |
| Cloud RF | | | | | | |
| Cloud LSTM/GRU | | | | | | |
| Lỗi local | | | | | | |
| Lỗi cloud (nếu chạy) | | | | | | |

Gửi lại output JSON của Verify/InspectRun và UUID của lượt có vấn đề. Không
cần gửi token, mật khẩu, kubeconfig, Secret hoặc log SDK thô.
