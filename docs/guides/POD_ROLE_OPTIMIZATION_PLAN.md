# Kế hoạch tinh gọn API/worker và giữ phục vụ trong triển khai

> Trạng thái 07/11/2026: gói này **tạm hoãn theo yêu cầu người dùng**, production
> đã rollback ngày06/11. Đọc [luồng CI/CD thống nhất](CICD_UNIFIED_FLOW.md) trước
> khi tiếp tục. Thiết kế chuẩn hóa tách migration một lần khỏi phát hành thường
> ngày; nội dung lịch sử bên dưới không tự mở lại Auto hoặc việc triển khai.

Ngày lập: **06/11/2026**, múi giờ Asia/Saigon.
Trạng thái: **đã được giao thực hiện; đang kiểm thử, chưa phát hành gói role**. Người dùng yêu cầu tối thiểu
dependency theo vai trò, giữ đầy đủ chức năng hiện hành và không downtime do
đợt triển khai. Source đang được tách theo vai trò; kiểm chứng image và triển khai được ghi riêng trong hồ sơ phát hành. Yêu cầu mới của người dùng chọn CD Auto cho gói này; đề xuất Manual trong kế hoạch ban đầu không còn là bước bắt buộc.

## 1. Mục tiêu và phạm vi

- Mỗi role chỉ mang mã, thư viện và extension cần trực tiếp hoặc gián tiếp cho
  nhiệm vụ của mình; tính cả import động và tải model/preprocessor lịch sử.
- Giảm image/pull/init, phụ thuộc nạp thừa và chi phí vận hành có số đo; giữ đủ
  năm model, inference, Model Quality, báo cáo SHAP/Feature Importance, Drift,
  điều phối Kaggle, theo dõi/dừng/nhận kết quả và lịch sử hiện hành.
- Phát hành từng gói tương thích ngược, giữ luồng phục vụ người dùng trong khi
  thay pod; lỗi bản mới không được làm mất bản đang phục vụ.
- Không thay thuật toán, schema DB, định dạng artifact/model hoặc tự Activate
  trong gói tinh gọn. Không gộp scale-to-zero, đổi hạ tầng dữ liệu hay hạ resource
  limit vào cùng lần chuyển đổi.

**Định nghĩa nghiệm thu:** không có request nghiệp vụ hợp lệ bị lỗi do rollout
(kể cả409 busy, 503, reset connection hoặc timeout), không mất yêu cầu đã được
xác nhận, không ngắt một training đang chạy hoặc submit Kaggle trùng. Training
mới có thể ở trạng thái queued trong lúc worker chuyển đổi, nhưng phải được lưu
bền vững và tự dispatch sau đó, không báo thành công giả hoặc yêu cầu user gửi lại.

Đây là mục tiêu cho thay đổi có kế hoạch này. Sự cố độc lập ở node01/Nginx/ngrok,
PostgreSQL, MinIO, MLflow hoặc đường mạng vẫn là phụ thuộc của kiến trúc hiện tại;
kế hoạch không biến chúng thành HA hoặc chứng minh không có mọi lỗi tương lai.

## 2. Những điểm cần bổ sung trước khi tối ưu image

Rà live chỉ đọc ngày06/11 ghi nhận:

| Hiện trạng | Ý nghĩa đối với kế hoạch |
| --- | --- |
| API2 replica, maxSurge0/maxUnavailable1, minReadySeconds0 | Hiện thay một pod trước khi có pod mới; cần cơ chế có ứng viên đã Ready trước khi rút bản cũ. |
| API chỉ được ở node02/node03; anti-affinity cứng theo app=traffic-api | Hai node đã có đủ hai pod. Chỉ đổi maxSurge1 có thể làm pod thứ ba Pending vì quy tắc placement cũ. |
| Grace period30s, không có preStop/drain hook | Chưa đủ bằng chứng request dài/upload/keep-alive được hoàn tất khi thay pod. |
| Readiness kiểm DB/MinIO/MLflow | Có ích nhưng chưa kiểm cache, active model, phiên bản preprocessor và khả năng dự báo. |
| API cache là emptyDir4Gi; restore từng pod48–55s trong lần đã chạy | Pod mới phải có cache/model hợp lệ trước khi nhận traffic; không bỏ restore chỉ để khởi động nhanh. |
| Worker mỗi role1 replica, strategy Recreate | Không được thay khi còn attempt hoặc cleanup/recovery chưa rõ. Cần lưu hàng chờ trong lúc thay worker. |
| CD guard giữ khóa DB chung mà đường nghiệp vụ cũng dùng | Có thể trả busy dù API vẫn Running. Hai replica/rolling update riêng lẻ không giải quyết được điểm này. |
| Chưa có PDB; Service API dùng NodePort30080, không publish NotReady | Cần rà endpoint/drain và chính sách bảo vệ khi node bị eviction; không coi PDB là bảo đảm rolling update. |

Tài liệu Nginx lịch sử trỏ node01→localhost:30080. Trước triển khai phải kiểm
cấu hình Nginx/ngrok thực đang nạp, keep-alive, timeout và endpoint propagation;
không lấy file lịch sử làm bằng chứng live cho toàn đường truy cập.

## 3. Phạm vi rút gọn theo vai trò

| Role | Phải giữ | Chỉ loại sau khi gỡ phụ thuộc chéo và kiểm tương thích |
| --- | --- | --- |
| API | HTTP/auth/DB/S3, điều phối/trạng thái, Drift, inference đủ5 model, Model Quality, đọc báo cáo | Executor/legacy fit, runner và gói gửi Kaggle chỉ worker cần; builder/fit bị kéo vào bởi module metrics/transform/schema. Giữ TF/Keras/sklearn/XGBoost/LightGBM nếu load/predict cần. |
| Worker cây | Kaggle/control/receipt/cancel, kiểm model cây, tổng hợp/pub kết quả các nhánh | TF/Keras và neural trainer nếu finalizer/helper chỉ cần metadata/artifact; API serving và legacy executor bị chặn. |
| Worker neural | Kaggle/control/receipt/cancel, TensorFlow/Keras, kiểm neural/preprocessor và publish branch | XGBoost/LightGBM, tree trainer và các module phục vụ API bị eager-import. |
| Sidecar đồng bộ artifact | S3, DB driver, cấu hình, khóa đồng bộ và retry/ack | Runtime ML, trainer, web API và các gói không phục vụ đồng bộ. |

Mã thuật toán vẫn cần ở repo và bundle gửi Kaggle, có thể đóng gói như archive
để worker vận chuyển, không nhất thiết import vào server. Không chỉnh sửa nội
bộ thư viện bên thứ ba để xóa riêng hàm fit.

Các điểm tách cụ thể:

- Chuyển metric thuần khỏi time_series_training để inference không kéo model
  registry/builders; tách transform phục vụ dự báo khỏi fit preprocessor.
- Tách context/storage/receipt/finalizer khỏi internal_training legacy.
- Tách schema/validator cấu hình khỏi engine tính SHAP. Nếu bỏ SHAP/numba/LLVM
  tại cluster, thay hợp đồng lấy version/profile hiện hành bằng lock được kiểm;
  không bỏ metadata check rồi làm cloud package sai runtime.
- Đánh giá XGBoost CPU thay GPU/NCCL, MLflow client nhẹ thay full server/UI nếu
  API thực dùng được đáp ứng. Giữ version/serialization đã kiểm; chưa chọn gói
  hoặc nâng dependency chỉ vì có version mới.

## 4. Thứ tự thực hiện và điểm dừng

### Giai đoạn A — Chốt baseline và chuẩn bị đường phát hành giữ phục vụ

1. Lưu baseline contract/output, image/UID/spec, Service/endpoint, trạng thái
   worker/DAG và resource headroom, không lưu credential. Chốt danh mục request
   và ngưỡng latency trước cutover; thống kê thành công theo nghiệp vụ, không chỉ health.
2. Tách khóa phát hành khỏi khóa đang chặn đường nghiệp vụ. Giữ ownership một
   release, CAS/inverse rollback và hàng rào chống dispatch/ownership xung đột.
   **Không chỉ bỏ guard** hoặc cho mọi thao tác đi qua trong lúc worker bị thay.
3. Quiesce execution: giữ training đang chạy hoàn tất, không hủy/khởi chạy lại;
   run mới được lưu queued và không dispatch vào worker sắp thay. Phân biệt
   queued hợp lệ với active/cleanup pending; không giữ luật cũ chặn mọi queued
   rồi làm deploy kẹt. Có reconciler khôi phục dispatch nếu runner mất kết nối.
   Nếu training vượt budget chờ của job, CD ghi rõ deferred/chưa triển khai và
   có cơ chế lên lịch lại; không giữ khóa chặn người dùng vô hạn, ghi completed
   giả hoặc cắt training để kịp timeout runner.
4. Rà các ghi nghiệp vụ tương thích trong lúc old/new cùng chạy. Controller chỉ
   kiểm invariant thuộc release; không yêu cầu toàn bộ dữ liệu người dùng đứng
   yên hoặc fingerprint toàn dataset/active-model phải bất biến suốt rollout.
5. Tạo và kiểm cơ chế ready/drain: cache/artifact/preprocessor hợp lệ cho model
   đang phục vụ; giữ readiness rẻ bằng trạng thái khởi tạo thay vì tính lại toàn
   bộ inference mỗi probe. Đổi active model phải có kiểm tải/cache đúng version.

**Điểm dừng A:** nếu vẫn cần trả busy cho request đang được hỗ trợ, không có
queued durable/recovery hoặc không chứng minh được old/new tương thích, chưa
phát hành gói tinh gọn với nhãn không downtime.

### Giai đoạn B — Tinh gọn source, giữ dependency/contract trước

Tách module và ranh giới role như mục3, chưa gỡ hàng loạt package. Tạo bảng
dependency giữ/bỏ/đổi cho từng role, gồm direct/transitive/dynamic import và lý do.
Kiểm những đường pickle/joblib/class import cũ trước khi đổi tên/chuyển module;
giữ shim tương thích khi cần. Không tạo artifact/model định dạng mới trong gói này.

Kiểm candidate trong môi trường cô lập bằng fixture và artifact mẫu được phép
dùng; app startup không tự train/submit Kaggle. So output dự báo/metric với
baseline, không chỉ kiểm import hoặc HTTP200.

### Giai đoạn C — Image theo role và dependency chuẩn bị sẵn

- Build các target API/worker cây/worker neural/uploader từ dependency đã chốt,
  dùng layer chung hợp lý. Base hiện tại~1,60GiB nén, payload mới nhỏ; cần base
  phù hợp, không xóa file trong pod hoặc chồng lệnh uninstall lên image cũ.
- Chỉ gỡ package đã chứng minh không cần cho role. TF vẫn giữ nơi dự báo/kiểm
  neural; sklearn/preprocessor và model libraries giữ khi artifact còn cần.
- Chuẩn bị SDK trong môi trường riêng ngay lúc build, giữ wheel lock/hash,
  đường interpreter và isolation đang dùng. SHAP chỉ mang tại nơi cần sau khi
  đã tách hợp đồng version, không trộn dependency gây lỗi TensorFlow/SDK.
- Kiểm image cuối và toàn overlay/mount/init; file cũ từ volume/base không được
  che image mới hoặc âm thầm đưa trainer trở lại. Secrets vẫn mount lúc chạy.
- Đo cold/warm pull, init, Ready, image nén/giải nén và tổng đĩa node. Nhiều image
  có thể làm tổng build/đĩa tăng nếu lặp layer; phải đo trước khi chốt phương án.

### Giai đoạn D — CI/CD hỗ trợ artifact theo role, giữ chính sách hiện hành

- Release manifest ghi digest, dependency contract, source/bundle identity theo
  role và resource/cohort được điều phối. Một giao dịch có thể có role giữ bản
  cũ vì không đổi; Verify phải kiểm đúng bảng role→digest, không báo sai commit.
  Giai đoạn chuyển base khai báo rõ cặp old/new được hỗ trợ cho từng role;
  không sửa hash hoặc nới wildcard để vượt kiểm baseline hiện hành.
- Giữ kiểm nguồn CI, test/import/startup, namespace UID, receipt, CAS và rollback.
  Bổ sung kế hoạch/receipt cho Service selector, canary, replica và drain, không
  có tài nguyên ngoài quản lý của transaction. Review quyền hẹp nếu cần tài nguyên mới.
- Giữ công tắc Manual/Auto, failure latch và Summary/bằng chứng30 ngày. Đề xuất
  dùng Manual cho đợt chuyển kiến trúc đầu tiên; **không tự đổi chế độ Auto đang
  bật khi mới lập kế hoạch**. Trước push thực thi phải chốt gói publish và mode
  để không tự rollout một commit nền tảng còn chưa tương thích.
- Không cắt regression suite để chạy nhanh. Cache đã có; tối ưu môi trường test,
  cache key gồm requirements được include, dependency layer và role thực sự đổi.
- Để gói worker sleep/scale-to-zero và tuning request/limit thành việc riêng sau
  khi image/đường phát hành mới đã được nghiệm thu.

### Giai đoạn E — Chuyển production từng bước, giữ bản cũ còn phục vụ

**API: dùng canary có kiểm soát cho lần chuyển đầu**, vì placement hiện tại chưa
cho phép surge cùng Deployment theo cách đơn giản.

1. Kiểm capacity CPU/RAM/request/đĩa và cache cho pod dư tạm thời, kể cả pod đang
   Terminating. Không đủ thì chưa triển khai; không hạ request/limit hoặc ép
   pod lên node điều khiển chỉ để vượt scheduler.
2. Chuẩn bị API ứng viên với selector/nhãn canary tách biệt, không xung đột
   anti-affinity cứng của pod cũ; Service thử riêng, chưa nhận traffic thật.
   Canary dùng DB/artifact contract tương thích, không tự Activate hoặc train.
3. Restore cache, kiểm model/preprocessor, warm active serving path, identity,
   readiness và ổn định trong khoảng đã chốt. Giữ hai API cũ phục vụ trong bước này.
4. Chuyển endpoint/traffic từng phần bằng cơ chế selector/cohort được kiểm trước;
   không giả định NodePort hiện có tự chia tỷ lệ canary theo ý muốn. Tránh bước
   selector trung gian không có endpoint; đo cả đường ngrok/Nginx/NodePort.
5. Chỉ rút bản cũ khi ứng viên đã có đủ capacity và qua nghiệm thu. Controller
   quản lý rõ Deployment/cohort nào nhận traffic; việc giữ/đổi tên canonical
   phải có bước tương thích các helper/Verify/RBAC, không bỏ lại canary ngoài CD.
6. Sau khi placement cho phép, steady-state hướng tới hai API trên hai node,
   RollingUpdate maxUnavailable0/maxSurge1, có minReadySeconds và readiness/drain
   đã kiểm. Điều kiện này không được áp mù vào topology cũ gây Pending.

Mỗi lần retire: loại endpoint khỏi nhận request mới, đợi propagation, hoàn tất
request đang xử lý và xác nhận các ghi/artifact đã được lưu; sau đó mới dừng.
Giữ sidecar đủ lâu để upload pending được ack, không cắt cả pod trước khi ghi
được bền vững. Grace budget căn theo request/upload dài nhất được hỗ trợ; hiện
30s chưa đủ bằng chứng, tài liệu Nginx lịch sử cho timeout tới600s. Không coi
ngủ vài giây trong preStop là đã chứng minh graceful shutdown.

PDB đề xuất minAvailable1 để bảo vệ eviction có kế hoạch; không bảo vệ mọi
rollout trực tiếp, nên vẫn cần strategy/drain và tránh node maintenance song
song với CD. Tổng tài nguyên tạm thời có thể vượt replicas+maxSurge khi pod cũ
còn Terminating, phải đưa vào capacity budget.

**Worker:** giữ API phục vụ; chỉ thay role đã idle và cleanup được xác nhận.
Không cho worker mới boot/claim cùng attempt đang do worker cũ giữ. Hàng chờ
được giữ bền vững, chỉ dispatch khi role đúng image/Ready và cả graph/finalizer
tương thích. Không tăng replica để chạy hai owner đồng thời cho cùng branch.
Canary worker chỉ kiểm startup/gate bằng fixture; không tự gọi provider thật.

**Sidecar:** thay cùng pod có drain/flush được kiểm. Không bỏ container đồng bộ
hoặc hợp đồng cache để đổi lấy pod Ready nhanh hơn. Không đổi dịch vụ dữ liệu
nền hoặc frontend nếu gói này không thay API contract.

### Giai đoạn F — Quan sát, nghiệm thu rồi mới thu hồi bản cũ

- Theo dõi request thực và probe nghiệp vụ được phép từ trước cutover, trong
  từng bước và sau ổn định; đề xuất tối thiểu10 phút trước/sau, điều chỉnh theo
  chu kỳ tải/latency đã thống nhất. Chạy cả keep-alive và request dài phù hợp.
- Không dùng health200 hoặc Ready2/2 làm bằng chứng duy nhất. Kiểm login/token
  qua old/new pod, API contract, đủ model/quality/report bằng fixture/artifact,
  upload/history và luồng queued/cancel/receipt không duplicate.
- Đo lỗi nghiệp vụ,409/503 do rollout, timeout/reset, p95/p99, endpoint và resource;
  chốt ngưỡng latency/tải trước cutover. Không che lỗi bằng retry vô hạn phía web;
  thao tác ghi phải có deduplication/idempotency phù hợp nếu retry.
- Tạo dữ liệu test/training/Kaggle quota thật chỉ khi được giao riêng; không
  dùng tài khoản/dữ liệu người dùng thật làm phép thử destructive hoặc Activate.
- Bản cũ chỉ thu hồi sau khi bản mới đạt cửa sổ quan sát. Giữ digest/config
  rollback và artifact cũ, không prune image/volume trong đợt tối ưu.

## 5. Dừng và rollback

| Điều kiện | Xử lý |
| --- | --- |
| Không đủ capacity, candidate Pending/NotReady, cache/model không hợp lệ | Dừng trước đổi traffic; bản cũ tiếp tục phục vụ. |
| Có lỗi nghiệp vụ do rollout hoặc latency vượt ngưỡng | Ngừng promote, chuyển traffic về cohort cũ còn Ready; giữ bằng chứng. |
| Có active attempt hoặc cleanup/ownership chưa xác nhận | Không thay worker; giữ/khôi phục hàng rào dispatch và kiểm phục hồi, không submit lại. |
| Runner mất kết nối hoặc spec/selector bị thay ngoài transaction | Dừng thao tác tiếp, giữ ownership/failure latch, kiểm thực trạng; không replay Apply mù. |
| Rollback ứng dụng | Chỉ hoàn tác resource/route thuộc giao dịch bằng UID/spec/selector CAS. Không hoàn tác dữ liệu hợp lệ người dùng đã ghi trong lúc canary chạy. |

Controller cần biết rollback traffic trước khi dọn candidate và không terminate
những pod đang là đường phục vụ cuối cùng. Rollback vẫn phải giữ drain/capacity;
release lỗi vẫn FAILED dù phục hồi thành công, Auto bị chặn theo chính sách.
Nếu chưa chứng minh đường rollback giữ phục vụ, chưa bắt đầu cutover.

## 6. Điều kiện chốt hoàn tất

1. Mọi chức năng hiện hành và model/artifact cũ đạt bộ kiểm tương thích.
2. Đường phát hành không giữ khóa khiến request người dùng trả busy.
3. Candidate/old/new/rollback được kiểm dưới tải đại diện, không có lỗi nghiệp vụ
   do rollout hoặc mất request/attempt; training mới queued rồi tự tiếp tục.
4. Có số đo trước/sau image, build, init, RAM/CPU và đĩa node; không công bố tỷ lệ
   tiết kiệm khi chỉ mới bỏ source hoặc xem idle snapshot.
5. Người vận hành xem được mode/role image/receipt và có phương án rollback đã
   kiểm; không yêu cầu tự chạy thêm kubectl ngoài workflow cho mỗi release.

## 7. Nguồn và bằng chứng

- Source hiện tại: scripts/cicd/deploy.py, maintenance.py; middleware/barrier
  vận hành; predictions.py và health.py. Việc giữ barrier cho các nghiệp vụ khác
  vẫn cần bảo toàn, không sửa/xóa hàng loạt vì thay cách khóa CD.
- Khảo sát local: results/cicd/pod-optimization-survey-2026-11-06.md và
  availability-plan-inventory.json. File Nginx ở Deployment_test_on_rke2 là
  tài liệu lịch sử, cần xác minh cấu hình đang nạp trước thực hiện.
- Kubernetes: [Deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/),
  [pod lifecycle/drain](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/),
  [PDB và giới hạn](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/).
- Docker: [multi-stage builds](https://docs.docker.com/build/building/multi-stage/).

Kế hoạch này chưa mở quyền triển khai hoặc đổi chế độ CD. Bước tiếp theo chỉ
thực hiện khi người dùng giao; không push code nền tảng vào Auto khi các điều
kiện giữ phục vụ còn chưa đạt.
