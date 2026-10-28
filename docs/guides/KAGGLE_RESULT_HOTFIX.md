# Bản vá tải kết quả Kaggle và trạng thái Skipped trong Airflow

Ngày 28/10/2026, sau khi người dùng áp dụng bản vá mạng đầu tiên:

- Lượt `ce187f2f-2335-4abb-b5bd-10d70952e17e` đã fit Random Forest, đánh giá
  và hoàn tất notebook Kaggle (`COMPLETE`, có `child_completed`). Lỗi sau đó
  khiến output chưa được chấp nhận; DB không đăng ký model từ lượt này, input
  còn và stop/cleanup đã xác nhận.
- Log lượt cũ chưa giữ đủ chi tiết lỗi tải. Bài RF + SHAP tự sinh 5.000 dòng
  chạy từ worker đã tái hiện `ConnectTimeout` tới `www.kaggleusercontent.com`.
  Receiver mới retry GET thành công, kiểm hash và nạp/predict CPU đạt, rồi dọn
  notebook/dataset. Không ghi DB/S3 nghiệp vụ hoặc đổi model active khi thử.

## Hai thay đổi

**Tải output:** RPC gửi bài và HTTP tải file là hai đường riêng. Receiver mới
chờ file được liệt kê sau COMPLETE, retry GET có giới hạn khi mất kết nối hoặc
signed URL hết hạn, làm mới URL của đúng version/file. Mỗi file vẫn phải qua
giới hạn kích thước/SHA/context; checksum sai thì dừng, không tự fit lại. Chỉ
xóa file tải dở do chính lời gọi tạo, giữ file đã tồn tại. Lỗi mới có operation/
category để InspectRun không còn nhận một nhóm lỗi trống cho đường tải này.

**Skipped thật trong Airflow:** trước đây worker trả `skipped` nhưng Python
task return bình thường, nên Airflow ghi Success xanh. Bản mới lưu receipt
rỗng hợp lệ vào XCom rồi phát `AirflowSkipException`, cho trạng thái Skipped
gốc của Airflow. Không chọn model không đồng nghĩa model đã train.

GRU và finalize dùng `none_failed`, cho phép nhánh được chọn chạy sau nhánh bỏ
qua; lỗi thật vẫn chặn finalize. Graph năm task và yêu cầu đủ ba receipt đúng
lượt vẫn giữ. Cách xử lý phụ thuộc phù hợp với
[quy tắc trigger của Airflow](https://airflow.apache.org/docs/apache-airflow/2.10.4/core-concepts/dags.html).

Đã chạy Airflow/XCom thật trong **SQLite tạm riêng**, có kiểm URL engine trước
initdb, với worker giả: RF-only → hai neural Skipped; GRU-only → cây/LSTM
Skipped nhưng GRU và finalize thành công; LSTM+GRU → cây Skipped; tree fail →
finalize upstream_failed. Không chạy task hoặc ghi metadata của Airflow dự án.

## Áp dụng

Gói mới: `results/cloud-training/cloud-result-fix`.
Bundle: `51e932afef3cceec54e4c3fc880823cad213ff954b90a59d670f3026ae5e6f6a`.
Chạy từng lệnh từ `main_project`, dừng nếu một lệnh lỗi:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\cloud_training\result-hotfix.ps1 -Action Check
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\cloud_training\result-hotfix.ps1 -Action Apply
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\cloud_training\result-hotfix.ps1 -Action Verify
```

Check chỉ đọc/server dry-run. Apply do người dùng chạy: kiểm idle, tạm pause
DAG, cập nhật volume mã của hai worker/API và hai Airflow pod, kiểm đủ replica
rồi trả pause về trạng thái ban đầu. Giữ image, flags Cloud Training đang bật,
migration, thuật toán/split/dữ liệu và frontend. Không cần publish frontend.

Giữ `result-fix-journal.json` nếu Apply dở dang; không replay Apply hoặc chạy
lại Stage/Enable/hotfix mạng cũ. Có action Rollback riêng khi không có lượt
training đang chạy/chờ dừng. Rollback khôi phục đúng volume trước gói này,
không xóa DB/model/dataset.

## Thử lại

Gửi **một lượt mới**, chỉ Random Forest, Cloud Training bật. Hai neural phải
hiện **Skipped màu hồng**; cây Success và finalize Success khi toàn lượt đạt.
Trạng thái lịch sử của lượt cũ vẫn giữ nguyên. Sau RF đạt mới kiểm neural.

“1/1 models evaluated” chỉ đếm đánh giá xong; vẫn phải nhận, kiểm CPU và lưu
kết quả rồi toàn lượt mới thành công. Khi thành công monitor trở về trống.

Nếu lỗi, lấy UUID và dùng lệnh chỉ đọc:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\cloud_training\rollout.ps1 -Action InspectRun -RunId 'UUID-CUA-LUOT-MOI'
```

Gửi JSON đã lọc và UUID; không gửi token hoặc signed URL. Kiểm bundle sau gói
này bằng `result-hotfix.ps1 -Action Verify`; helper Verify cũ đối chiếu bundle cũ.
