# Model artifact cache trong release Kubernetes

Đây là hợp đồng source ứng viên sau khảo sát lỗi restore-cache/CD45, chưa là
nghiệm thu production. Dùng workflow release hiện có và đúng artifact/SHA;
không mở chốt, replay release lỗi hoặc sửa receipt để đổi cache.

## Mục tiêu và cấu hình

- API emptyDir6Gi, restore/disk cache budget5.153.960.755byte (80%), tối đa
 20000file restore. Phần còn lại dành cho hoạt động khi chạy; không là bảo
  đảm đĩa node đã giữ chỗ hoặc lịch sử metadata không bao giờ vượt giới hạn.
- API ephemeral request5Gi/limit7Gi; sidecar256Mi/1Gi; restore init5Gi/7Gi.
  Effective storage request5,25Gi tính init tuần tự và native sidecar; không
  cộng trùng request của restore init với request steady-state như cùng chạy.
- Checker dự trù `max(disk emptyDir sizes, effective ephemeral request)` cộng
 512Mi mỗi pod staging, sau đó vẫn giữ reserve node từ runtime contract12Gi.
  CPU/RAM requests giữ cách cộng bảo thủ cũ. Scheduler admission vẫn quyết định
  đối với workload ngoài namespace; free disk là snapshot, không là reservation.

## Restore và phục vụ model

API restore init khai báo `TRAFFIC_CACHE_RESTORE_MODE=active`. Trước tải object,
SQL read-only chọn model của region active và neural preprocessor cần thiết.
Inventory giữ binary được tham chiếu và mọi model metadata/file không thuộc
các định dạng binary đã nhận diện; năm prefix results/monitoring/mlruns/
data_versions/data vẫn giữ toàn bộ. Binary `.pkl/.joblib/.keras/.h5/.hdf5/
.onnx/.pt/.pth` lịch sử không bắt buộc restore lúc startup.

Thiếu/empty artifact bắt buộc, reference sai, listing lỗi/không hoàn chỉnh
hoặc vượt budget đều dừng trước download. Report chỉ chứa số lượng/byte/mode,
gồm skipped_model_files/bytes, không có tên object/ID model. Quét tối đa100000
object và60giây quan sát; timeout S3 còn phụ thuộc timeout/retry client.

Preflight đọc mode từ manifest và thực thi source candidate trên sidecar cũ;
không cần image cũ đã có module cache mới. Init tái list và chọn dependency
khi pod bắt đầu. Không có atomic snapshot hay reservation giữa hai lần đọc.
`full` là chế độ rõ ràng cho caller cũ, không phải fallback khi `active` lỗi.

Serving runtime đã có tải model thiếu và warm active; gói mới thêm budget đĩa
và phối hợp cache. Mỗi miss đọc lại tập active để pin, qua cleanup barrier;
HEAD kiểm size/identity, GET có IfMatch, kiểm size/checksum rồi atomic replace.
Model/preprocessor phải load được trước serving-ready. Tập pin là snapshot
của DB, không là khóa distributed ngăn Activate trên replica khác; cache miss
sau thay active vẫn tải lại được từ MinIO. Không tự Activate hoặc train.

## Disk cache và độ bền dữ liệu

`TRAFFIC_ARTIFACT_CACHE_BYTES` bật bounded cache ở API/sidecar.
Helper flush/inventory được thực thi trong image legacy phải giữ khả năng
nạp bằng stdlib; chỉ import module disk cache khi có biến opt-in trên pod mới.
Fixture thay PVC log bằng emptyDir cũng phải có sizeLimit để qua cùng checker
disk staging, không bỏ kiểm cho môi trường test.

Metadata local nằm trong models/.traffic-cache, được loại khỏi uploader/flush và không được
restore từ object store. Khóa file trên volume dùng chung giữa process/container,
giữ suốt lúc model/preprocessor loader đọc file, upload và explicit flush.
Lock contention có deadline; không cam kết inference không bao giờ trả503.

LRU trên đĩa chỉ xét binary đã được download kiểm chứng hoặc upload ổn định
có SHA metadata khớp remote HEAD. Trước thu hồi phải khớp fingerprint, SHA256,
remote ETag và size; pin active và file của request hiện tại. File unmanaged,
dirty, chưa ack hoặc remote đã đổi/mất không cho phép eviction. Chỉ unlink bản
sao local; không gọi DELETE object và không bỏ lịch sử model trong DB/MinIO.
Metadata/dataset không tự evict; nếu chúng hoặc tập pinned vượt budget thì dừng.

Uploader tránh upload lại binary sạch còn đúng identity, giữ kiểm cleanup/
tombstone khi reconcile. Background uploader có thể prune sau ack dưới cùng
cleanup barrier. Explicit flush không prune dưới inventory before/after;
chỉ xác nhận retirement khi tập file ổn định và đầy đủ acknowledgement.
Eviction giới hạn128remote probes/30giây quan sát, thêm timeout client S3.
Đĩa runtime vẫn có thể thiếu do writer khác; lỗi download hữu hạn giữ file
cũ và không publish file tạm lỗi. Full volume/log/dirty data cần xử lý riêng.

## Audit reader và kiểm chứng

| Đường đọc | Cách giữ chức năng khi không restore binary lịch sử |
| --- | --- |
| Tree inference/model quality | predictions._load_model qua PredictionRuntime.load_model, tải thiếu trước loader. |
| Neural inference/model quality | Wrapper cho model Keras và preprocessor, cùng khóa cache; không đổi toán học inference. |
| SHAP/feature importance | report_bytes giữ local metadata JSON và đã có fallback GET S3; không evict metadata. |
| Dataset phục vụ prediction/drift | Đọc storage_uri S3 qua đường hiện có; prefixdata cũng tiếp tục restore. |
| Results/monitoring/mlruns/data_versions | Tiếp tục restore đầy đủ; chưa giảm phạm vi các reader này. |
| Worker training/finalize | Scratch/receipt riêng theo role, không đổi sang cache API hoặc chạy provider để nghiệm thu. |

Tests synthetic: chọn active/preprocessor, lịch sử lớn, thiếu required, corrupt
download, pin/dirty/remote overwrite, LRU, khóa giữa process, upload/flush và
tombstone, manifest budget/resource, staging disk shortfall. Test loader tree/
neural dùng bytes giả không thay nghiệm thu model thật đủ5family. CI full và
current Kubernetes vẫn bắt buộc; kiểm local thiếu TensorFlow không thay chúng.

Khi được giao phát hành: CI artifact mới → inventory/Plan mới → Manual gate →
Apply+Verify → kiểm inference/DAG/model active/queue → lưu receipt. Đo cùng
điều kiện: byte tải startup, thời gian tới ready/stage, peak disk, cache miss
latency và lượng tải lại. Chưa có phần trăm cải thiện production được công bố.

## Nguồn thiết kế

- [Kubernetes ephemeral storage](https://kubernetes.io/docs/concepts/storage/ephemeral-storage/):
  request/limit, emptyDir/log/writable layer và eviction.
- [Kubernetes emptyDir](https://kubernetes.io/docs/concepts/storage/volumes/#emptydir):
  dữ liệu theo vòng đời Pod và chia sẻ đĩa node.
- [KServe S3/MinIO](https://kserve.github.io/website/docs/model-serving/storage/providers/s3),
  [kserve-storage repository](https://github.com/kserve/kserve/blob/master/python/storage/README.md):
  artifact URI và tải model vào thư mục phục vụ.
- [KServe local model cache](https://kserve.github.io/website/docs/model-serving/generative-inference/modelcache/localmodel):
  tham khảo prewarm/cache, chưa cài KServe/PV/agent trong gói này.
- [Seldon multi-model serving](https://docs.seldon.ai/seldon-core-2/v2.8/models/mms):
  tham khảo working set; LRU RAM của Seldon khác disk cache do dự án triển khai.

6Gi/80% và reserve là cấu hình dự án cần Plan/đo lại, không là chuẩn từ repo mẫu.
