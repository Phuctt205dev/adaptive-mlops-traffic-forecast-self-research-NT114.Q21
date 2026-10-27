# Xóa vùng và dữ liệu thuộc vùng

Bản cập nhật mở rộng DELETE admin hiện có, không xây lại pipeline training.
Chỉ admin được xóa. Không có thao tác tự xóa vùng khi rollout hoặc kiểm tra.

## Luồng xử lý

1. Kiểm tra không còn train, dự báo hoặc đồng bộ file đang thực thi. Các
   tác vụ bình thường cùng giữ khóa chia sẻ nên vẫn chạy song song; xóa
   dùng khóa độc quyền và trả lỗi để thử lại nếu hệ thống đang bận.
2. Đối chiếu quyền sở hữu bằng UUID vùng/lượt train và metadata. Kiểm tra
   Airflow, MLflow, kho file và đủ hai API + hai worker trước khi xóa.
3. Lưu danh sách cần dọn vào AuditLog, vô hiệu hóa vùng. Journal này tồn tại
   qua restart; chưa xóa các bản ghi chứa vị trí file. Trong thời gian xóa
   hoặc chờ thử lại, các request nghiệp vụ tạm dừng; health/login và danh
   sách vùng vẫn chạy để admin có thể mở lại trang và nhấn Delete thử lại.
4. Dọn file và cache ở bốn pod, log Airflow của từng run thuộc vùng, đánh
   dấu xóa experiment MLflow, xóa DAG run đã kết thúc và file trên MinIO.
   Nếu bucket bật versioning, xóa cả phiên bản và delete marker trong prefix.
5. Kiểm lại kho file và danh tính pod. Xóa drift, prediction, model,
   training run, dataset rồi vùng trong một transaction DB. Lưu tombstone
   để sidecar không upload lại file cũ từ pod thay thế.

Nếu một bước lỗi, giao diện báo chưa hoàn tất. Nhấn Delete lại đúng vùng
để tiếp tục; không cần tạo lại vùng hoặc sửa trạng thái train bằng tay.
Vùng đang dọn vẫn thấy trong danh sách admin có include_inactive=true.
Không tự đánh dấu một lượt queued cũ thành completed: chỉ cho dọn khi
Airflow xác nhận run đã kết thúc/không còn và không có tác vụ chạy.
Các lần thử Airflow cũ có mã training_run_id không phải UUID chỉ được dọn
metadata/log nếu xác định đúng vùng; mã lỗi không được biến thành đường
dẫn file hoặc prefix S3. File không xác định được chủ sở hữu được giữ lại.

## Những gì được dọn

- DB ứng dụng: DriftCheck, Prediction, ModelVersion, TrainingRun, Dataset,
  các con trỏ active/recommended, cuối cùng Region. Chặn tham chiếu chéo vùng
  trước khi xóa bất cứ file nào.
- Dataset gốc và quality report tại regions/<region UUID>/ trên bucket dataset.
- Cây/thư mục model legacy, recurrent, distributed-serving, distributed-training,
  dữ liệu trung gian, SHAP, file upload cache và training-exchange đúng UUID.
- Airflow: DAG run training thuộc vùng, kể cả run tìm thêm qua conf, và
  thư mục log dag_id=.../run_id=... tương ứng. Không xóa DAG hoặc lịch sử
  drift chạy chung nhiều vùng. Worker recurrent được mount PVC log đang ở
  cùng node03; không thay Deployment Airflow.
- MLflow: kiểm experiment không trộn vùng; chấp nhận neural legacy thiếu
  tag chỉ khi experiment có tên vùng duy nhất. Đánh dấu xóa metadata qua API
  chính thức và xóa vật lý prefix artifact đã xác minh trên MinIO.
  Metadata MLflow trong thùng rác và journal/tombstone quản trị được giữ;
  không chạy GC toàn cục hoặc xóa trực tiếp DB MLflow.
- RAM: bỏ tham chiếu cache model/báo cáo/dataset. Cache functools phải clear
  toàn cache nhỏ vì không hỗ trợ xóa từng key; dữ liệu bền vững của vùng khác
  được giữ. gc.collect không bảo đảm allocator/TensorFlow trả toàn bộ RSS
  ngay cho hệ điều hành, cũng không bảo đảm tránh OOM ở lần train sau.

Không xóa dependency SHAP dùng chung, snapshot/review, file không xác định
được chủ sở hữu, hoặc dùng tên vùng/chuỗi chứa UUID để quét và xóa tùy ý.
Đường dẫn ngoài root, symlink, tham chiếu chéo và experiment không rõ chủ
sở hữu đều bị chặn. Không báo thành công nếu S3 trả lỗi xóa từng object.

## Kiểm chứng và triển khai

- tests/test_region_cleanup.py dùng SQLite bật foreign key, storage/peer giả
  và thư mục tạm; kiểm xóa drift, giữ vùng khác, retry, ownership, path traversal.
- tests/test_region_service.py tiếp tục kiểm service xóa hiện có với adapter giả.
- Deployment_test_on_rke2/cong-cu/audit-training-residue.py chỉ đọc trạng thái
  Airflow/DB/process và thống kê file, không train hoặc xóa thật.
- Bộ triển khai riêng nằm tại
  Deployment_test_on_rke2/tai-lieu/README-region-cleanup.md. Chưa apply thì
  source local không thay hành vi của hệ thống đang chạy. Không deploy riêng
  mỗi API: barrier/uploader cần đồng bộ cả hai API và hai worker.
- Bản không có runtime cleanup trả lỗi cấu hình trước khi xóa; không lùi về
  xóa DB đơn thuần. Không có migration DB hoặc dependency Python mới.

Nguồn cơ chế: [khóa advisory PostgreSQL](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS),
[Airflow REST API](https://airflow.apache.org/docs/apache-airflow/2.10.4/stable-rest-api-ref.html),
[MLflow delete experiment](https://mlflow.org/docs/latest/api_reference/rest-api.html#delete-experiment)
và [MLflow GC](https://mlflow.org/docs/latest/api_reference/cli.html#mlflow-gc).
Các nguồn giải thích cơ chế, không chứng minh trạng thái cụm của dự án.
