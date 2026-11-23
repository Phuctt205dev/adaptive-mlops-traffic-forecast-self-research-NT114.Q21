# Xóa vùng và dữ liệu thuộc vùng

Bản cập nhật mở rộng DELETE admin hiện có, không xây lại pipeline training.
Chỉ admin được xóa. Không có thao tác tự xóa vùng khi rollout hoặc kiểm tra.

## Luồng xử lý

1. Kiểm tra không có Airflow đang chạy. Khi admin xóa, hệ thống dành quyền
   ưu tiên rồi chờ tối đa20 giây để tác vụ đang dùng file kết thúc. Đồng bộ
   file nhường quyền bằng cách dừng an toàn lượt upload của chính nó;
   không hủy training hoặc ép giải phóng khóa của tiến trình khác.
   Tác vụ bình thường vẫn chạy song song khi không có yêu cầu xóa.
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
  thiết lập auto-retrain của vùng (AuditLog `region_drift_settings_updated`),
  các con trỏ active/recommended, cuối cùng Region. Journal dọn vùng được giữ
  vì nó làm cho việc gửi lại Delete sau khi mất phản hồi an toàn. Chặn tham chiếu chéo vùng
  trước khi xóa bất cứ file nào.
- Dataset gốc và quality report tại regions/<region UUID>/ trên bucket dataset.
- Các multipart upload còn dở trong đúng prefix sở hữu, gồm phần còn sót
  sau upload lỗi. Uploader tự nhường quyền và sẽ thử lại sau đợt xóa.
  Việc kiểm kê có giới hạn trên toàn bucket rồi lọc prefix trong ứng dụng,
  để xử lý trường hợp dịch vụ lưu trữ trả thiếu kết quả khi lọc trên server.
- Cây/thư mục model legacy, recurrent, distributed-serving, distributed-training,
  dữ liệu trung gian, SHAP, file upload cache và training-exchange đúng UUID.
- Thư mục tạm của Local training `local-training/v1/<run>/` của từng lượt train thuộc vùng
  (và của lượt train của model bị xóa riêng). Bình thường Local training tự dọn nó sau mỗi
  attempt; đưa vào kế hoạch xóa để phần dư của attempt bị gián đoạn không bị bỏ sót.
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

### Xóa riêng một model

Model phát hành qua Local/Kaggle có thư mục riêng theo UUID vùng/lượt train.
Khi xóa, hệ thống kiểm model khác không cùng dùng thư mục, preprocessor hoặc
MLflow run đó; chỉ nhận layout và tag xác định được chủ sở hữu. Layout legacy
không chứng minh được quyền sở hữu bị chặn trước khi xóa, cần kiểm kê riêng.

Journal model dùng chung barrier và uploader với xóa vùng, lưu tiến độ trước
khi dọn cache, đánh dấu xóa đúng MLflow run và xóa artifact MinIO. Không xóa
MLflow experiment, dataset, DAG run hoặc lịch sử drift của vùng khi chỉ xóa
model. Sau Verify mới xóa bản ghi model/prediction và nhả các tham chiếu;
giữ lịch sử training/drift. Nếu admin xóa đúng model đang phục vụ, con trỏ
active được nhả như trước; cần Activate model khác để dự báo tiếp.

Lỗi ngoài DB giữ inventory và chặn công việc xung đột cho tới khi admin Retry;
không báo thành công vì chỉ đã xóa bản ghi. Tombstone chặn uploader/flush và
dọn cache ở pod thay thế để file đã xóa không bị tải lên lại.

GET `.../regions/{id}/cleanup-status` hoặc
`.../model-versions/{id}/cleanup-status` yêu cầu quyền admin, chỉ trả bước,
trạng thái, thời điểm cập nhật và khả năng thử lại; không trả đường dẫn lưu trữ,
địa chỉ peer hay lỗi thô. GET này đọc được khi coordinator đang giữ khóa.
F5/mất phản hồi chỉ đối soát GET; không tự gửi thêm lệnh xóa.

Dung lượng object không đồng nhất với Disk toàn node: PostgreSQL giữ trang
đã cấp, image/log thuộc phần khác và MinIO dọn trash ở nền. Không xóa tay
`.minio.sys`. [Source MinIO](https://github.com/minio/minio/blob/master/cmd/erasure-sets.go)
mô tả vòng dọn trash định kỳ; thời gian thực tế còn phụ thuộc tải/phiên bản.

Kiểm synthetic mới: `tests/test_model_cleanup.py` và
`scripts/maintenance/check_cleanup_ui.py`; không chạy xóa/train production
chỉ để nghiệm thu code.

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

## Trường do hệ thống quản lý khi tạo lượt train

`artifact_root` (`models/regions`) và `model_role` (`candidate`) hiển thị chỉ đọc
trong Training Configuration → Advanced. Backend cũng cố định hai giá trị này:
`TrainingRunCreate` thay mọi giá trị do người gọi API gửi bằng giá trị hệ thống,
nên không thể chọn thư mục ghi kết quả hoặc vai trò model. Train Kaggle/Local vốn
dùng thư mục output riêng và vai trò `candidate`, hai ô này không điều khiển
huấn luyện. Cấu hình đã lưu từ trước vẫn đọc được. Kiểm tra:
`tests/test_training_configuration.py` (`SystemManagedFieldTests`).

## Kiểm kê dữ liệu của lượt train không còn model (chỉ đọc)

Xóa một model bằng bản cũ chỉ gỡ bản ghi, không xóa file; những file đó còn lại trong kho
và không có thao tác nào trên giao diện dọn được (không có chức năng xóa riêng lượt train).
`scripts/maintenance/unreferenced_run_data.py` **chỉ liệt kê**, không xóa hay ghi gì:

- Với mỗi lượt train có file trong kho: dung lượng theo từng vị trí (serving, exchange,
  training, local-training, thư mục legacy, MLflow), và kết luận **REMOVABLE** hoặc **KEEP**
  kèm lý do (còn model dùng, lượt đang chạy, execution chưa xong, đang có thao tác xóa dở,
  một model đang dùng nhắc tới file của lượt này).
- Tách riêng các MLflow run không có tag lượt train: không chứng minh được chủ sở hữu nên
  không bao giờ là ứng viên. Cũng liệt kê thư mục của lượt train không có trong DB nếu có.
- Không bao giờ là ứng viên: model của mọi vùng và file chúng phục vụ, SHAP dùng chung,
  dataset/quality report, thư mục model legacy theo vùng.

Chạy trong pod API: `kubectl exec -i <api-pod> -c api -- sh -c 'cd /app && PYTHONPATH=/app python -B -' < scripts/maintenance/unreferenced_run_data.py`
(thêm `--json` để in bản JSON). Việc xóa thật là bước riêng, ở mục dưới.
Kiểm tra: `tests/test_unreferenced_run_data.py` (kho và MLflow giả chỉ có thao tác liệt kê,
nên test chứng minh công cụ không thể ghi).

## Xóa file của lượt train không còn model

`scripts/maintenance/purge_unreferenced_run_data.py` xóa file của **đúng các lượt train được chỉ
định** (mỗi lượt phải ghi đủ UUID, lấy từ báo cáo ở trên; không có chế độ tự chọn). Mặc định là
chạy thử: thực hiện mọi bước kiểm tra, in sẽ xóa những gì, không đổi gì. `--execute` mới xóa thật,
từng lượt một:

- Dùng lại cơ chế của xóa model: journal có thể tiếp tục khi gián đoạn, khóa DB chung, kiểm
  sức khỏe đủ hai API và hai worker, cache của bốn pod bị xóa, file đã xóa không thể bị pod
  còn bản local đẩy ngược lên kho (tombstone), MLflow run bị đánh dấu xóa rồi file bị xóa khỏi
  MinIO, bước Verify đọc lại kho.
- Từ chối (HTTP 409 `run_cleanup_conflict`, không đổi gì) khi lượt train còn model dùng, còn model
  đang dùng nhắc tới file của nó, đang chạy hoặc execution chưa xong, hoặc có một thao tác xóa
  khác đang dở. Một MLflow run không chứng minh được thuộc lượt train, thuộc vùng khác, đang chạy
  hoặc đang được model dùng cũng làm cả lượt bị từ chối.
- Bản ghi lượt train và lịch sử của nó được giữ; chỉ file bị xóa. Gọi lại sau khi xong là không
  làm gì thêm.

Chạy trong pod API (sau khi bản có công cụ đã được triển khai):
`kubectl exec -i <api-pod> -c api -- sh -c 'cd /app && PYTHONPATH=/app python -B - --run <uuid> [--execute]' < scripts/maintenance/purge_unreferenced_run_data.py`.
Kiểm tra: `tests/test_run_data_cleanup.py`.
