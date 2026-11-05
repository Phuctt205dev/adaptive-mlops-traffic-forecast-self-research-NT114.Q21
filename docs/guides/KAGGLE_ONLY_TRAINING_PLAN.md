# Kế hoạch chuyển training sang chỉ dùng Kaggle

Ngày lập: 05/11/2026, Asia/Saigon. **Trạng thái: source đã thực hiện và qua core/browser/startup/dry-run; chưa publish hoặc triển khai production.** Bản kế hoạch ban đầu chỉ để review; chỉ dẫn tiếp theo đã cho phép thực hiện cùng gói tối ưu API. Bằng chứng kiểm thử và phạm vi tối ưu kèm theo ở mục 8.

## 1. Kết quả mong muốn

- Mọi **lượt train mới**, train lại thủ công và Drift auto retrain đều dùng Kaggle.
- Modal vẫn cho chọn RF/XGBoost/LightGBM/LSTM/GRU và giữ các tham số hiện có; bỏ công tắc chọn local/cloud.
- Backend mặc định Kaggle khi request không ghi backend. Request chỉ rõ local bị từ chối rõ ràng, không được âm thầm đổi backend của cùng một run.
- Worker/Airflow không còn đường thực thi fit trên cụm, kể cả gọi endpoint cũ hoặc tạo conf trực tiếp. Kaggle không khả dụng thì báo lỗi/không nhận việc; không fallback local.
- Model đã train local, lịch sử, kết quả, dataset và active model vẫn đọc/dự báo được. Không sửa lịch sử để biến run local thành Kaggle.

## 2. Những gì giữ nguyên

- Hai API tiếp tục phục vụ người dùng, xác thực, tạo yêu cầu, monitor, dự báo và Model Quality.
- Hai worker tiếp tục chuẩn bị input, gửi job Kaggle, theo dõi/dừng, nhận/kiểm output và tổng hợp kết quả. Cloud-only không đồng nghĩa xóa hai worker.
- Luồng nhánh cây song song nhánh neural, LSTM trước GRU, receipt/artifact verification, tiêu chí chọn model, CV/Final Test, SHAP và feature selection giữ nguyên.
- Mã fit vẫn cần trong gói chạy trên Kaggle và trong kiểm thử thuật toán. Dependency nạp model cây/neural còn cần cho inference/kiểm output; không gỡ TensorFlow hoặc thư viện ML chỉ vì bỏ local training.
- Không đổi schema DB, format model, dataset hay tự Activate. Tách sạch toàn bộ image API/worker và tối ưu cache-sync là các gói riêng, không gộp ngầm vào lần chuyển backend này.

## 3. Các điểm thay đổi dự kiến

| Lớp | File/điểm đọc hiện tại | Thay đổi dự kiến |
| --- | --- | --- |
| Giao diện | `frontend/src/main.jsx`, style công tắc liên quan | Bỏ state/control Cloud Training; luôn gửi Kaggle; giữ thông báo nơi train. Có trạng thái đang kiểm cấu hình cloud, không khả dụng và lỗi submit; bỏ mọi gợi ý tắt cloud để chạy local. |
| Request API | `backend/app/schemas/model_version.py`, `backend/app/api/routes/datasets.py` | Chỉ chấp nhận backend Kaggle cho run mới; thiếu backend mặc định Kaggle. Kiểm cloud/control đã bật trước tạo queued run; local nhận lỗi có ý nghĩa. |
| Capabilities | `backend/app/api/routes/training_monitor.py` | Default backend là Kaggle, thể hiện chỉ hỗ trợ cloud và không fallback. Không coi cờ cấu hình bật cloud là bảo đảm quota/provider đang khỏe. |
| Trạng thái execution | `backend/app/services/training_execution.py` | Chỉ tạo execution Kaggle mới, chặn bypass schema. Đọc/hiển thị record local lịch sử vẫn tương thích. |
| Retrain | `backend/app/services/drift_monitoring.py` và các caller retrain | Run mới luôn Kaggle, kể cả model nguồn từng train local hoặc metadata cũ thiếu backend. Giữ nguyên cấu hình thuật toán/feature hợp lệ; tạo run/config mới, không đổi config/hash của run cũ. |
| Worker controller | `backend/app/services/training_control.py` | Chặn start local trước claim/thực thi; bỏ nhánh spawn subprocess fit local. Giữ cloud execution, cancellation, publication/receipt và kiểm output phục vụ inference. |
| Route thực thi cũ | `deployment/rke2/runtime/cloud_training_runtime.py` và router liên quan | Chặn các đường execute local cũ, kể cả protocol cũ. Kiểm cả endpoint execute đơn và execute theo nhánh; giữ finalization/receipt cần cho cloud, không xóa nhầm. Serving gate trên API vẫn giữ. |
| Airflow | `deployment/rke2/runtime/traffic_region_training_cloud.py`, `src/cloud_training/airflow_control.py`, validator/DAG legacy được bọc | DAG chỉ chấp nhận luồng Kaggle có controlled protocol. Không rẽ sang legacy local khi thiếu/khác protocol; conf cũ không an toàn bị từ chối trước dispatch. Giữ task graph/receipt contract hiện hành. |

Thay đổi mặc định phải áp dụng ở điểm tạo run; không đơn thuần thay tất cả chuỗi `local` trong repository. Các nhãn lịch sử, kiểm thử thuật toán và đường recovery của lịch sử cần được phân loại riêng.

## 4. Luồng lỗi và chuyển tiếp

1. **Thiếu cấu hình cloud/control:** API không tạo run mới; web thông báo training cloud chưa sẵn sàng.
2. **Kaggle hết quota/lỗi mạng/provider:** trạng thái và thông báo theo lỗi cloud, không khởi chạy local. Việc nhận hoặc chặn ở web dựa trên capabilities không thay cho xử lý lỗi thật của provider.
3. **Client cũ gửi local:** từ chối có thông báo chuyển sang Kaggle. Client cũ gửi Kaggle hoặc không ghi backend vẫn có đường chuyển tiếp theo API contract mới.
4. **Local run đang queued/running ở thời điểm phát hành:** rà trước cutover; đợi hoàn tất hoặc lập phương án hủy riêng có xác nhận. Không đổi backend của run đang tồn tại rồi tiếp tục cùng ID, không tự hủy hàng loạt hoặc bỏ receipt/lock.
5. **Run local đã kết thúc:** giữ đúng thông tin nguồn local; tải model, đọc báo cáo và dự báo vẫn hoạt động.

## 5. Thứ tự thực hiện khi được giao làm

### Gói backend, worker và Airflow

- Chốt policy Kaggle-only ở mọi điểm tạo/dispatch/thực thi; cập nhật test tương ứng và guide.
- Kiểm hiện trạng không còn công việc đang chạy trước khi cutover; không sửa dữ liệu để vượt preflight.
- Kiểm app startup/gate/inference trên ba role và kiểm import/task graph Airflow với DAG mới. Không chỉ dựa vào unit test hoặc server dry-run.
- Backend CI kiểm/build artifact theo commit/digest, người dùng duyệt deployment như hiện tại, rồi rollout/Verify. Có thể phải cập nhật cả Airflow do DAG/control thay đổi.

### Gói frontend sau khi backend đã xác nhận

- Publish modal không còn công tắc, chỉ dẫn Kaggle rõ ràng, xử lý trạng thái unavailable và lỗi submit.
- Kiểm browser/mobile, chọn 1 hoặc nhiều model, Train và Retrain.
- Nếu cần giữ thứ tự backend trước frontend, publish bằng hai gói/commit tách nhau. Push đồng thời code frontend/backend có thể khiến Pages cập nhật trước backend đang chờ duyệt; phải tính đến hành vi này trong release plan.

Không đổi cơ chế duyệt CD sang tự động hoàn toàn. Mỗi gói phát hành có bản đã kiểm chứng để hoàn tác; không chạy Apply tay song song workflow.

## 6. Các ca nghiệm thu bắt buộc

| Ca | Kết quả mong đợi |
| --- | --- |
| Mở modal Train/Retrain | Không có công tắc local/cloud; hiện Kaggle, chọn model vẫn đủ năm loại. |
| Request không ghi backend | Backend tạo cấu hình Kaggle. |
| Request chỉ rõ local/backend khác | Bị từ chối trước tạo queued run/dispatch. |
| Worker start/conf Airflow local hoặc legacy | Bị chặn, không spawn process fit, không tạo notebook/job thay thế âm thầm. |
| Retrain từ model local cũ hoặc metadata thiếu backend | Run mới là Kaggle, lịch sử cũ không đổi. |
| Cloud tắt/không sẵn sàng/provider lỗi | Thông báo lỗi phù hợp; không fallback local. |
| Chọn riêng cây, riêng neural hoặc đủ năm | Nhánh không chọn có skipped receipt đúng; graph/finalize không đợi output không tồn tại. |
| Theo dõi, hủy, retry/deduplicate, nhận kết quả | Giữ owner/attempt/lease, receipt/hash và cơ chế publication hiện hành. |
| Model cũ được train local | Dự báo, Model Quality và Feature Importance vẫn đọc được. |
| CI/build/startup | Đủ kiểm thử bắt buộc và import/gate/schema/DAG checks; không skip ca lỗi chỉ để build qua. |

Test pipeline bằng fixture nhỏ/offline không đồng nghĩa giữ tính năng local training trong sản phẩm. Test cũ xác nhận local runtime cần chuyển thành test từ chối hoặc test tương thích lịch sử tùy mục đích, không xóa đồng loạt.

Smoke train Kaggle thật có dùng quota/GPU hoặc tạo tài nguyên cloud chỉ chạy nếu được người dùng giao riêng; không tự chạy khi mới lập kế hoạch hoặc coi unit test là nghiệm thu provider thật.

## 7. Ranh giới quyền ở mốc hiện tại

Người dùng đã giao thực hiện theo kế hoạch, bao gồm phát hành backend trước rồi frontend; tiến độ/bằng chứng nằm trong `My-worklog/hand-offs/2026-11-05-system-optimization.md`. Backend đã qua cổng duyệt của người dùng và triển khai thành công. Chưa có quyền chạy training thật để nghiệm thu; không dùng thành công triển khai làm bằng chứng đã kiểm Kaggle provider/quota thật.

## 8. Phần source đã làm và bằng chứng trước publish

- UI bỏ công tắc, luôn gửi Kaggle, giữ năm model; API/worker/Airflow/runner đều chặn local. Retrain từ metadata local cũ tạo run Kaggle mới, không đổi lịch sử.
- API không còn đăng ký hoặc import internal_training/executor. Vẫn giữ inference/Model Quality và dependency load model; gói đầu giữ pinned base đã kiểm chứng, chưa tuyên bố image đã được làm nhỏ hoàn toàn.
- Uploader tăng dần: chỉ đánh dấu file ổn định đã upload thành công, retry khi lỗi/thay đổi và đối soát định kỳ; skip file không đổi trước DB barrier, giữ cleanup fence. Entrypoint release riêng đưa thay đổi vào sidecar thực chạy; patch image/command/probe có inverse để rollback.
- API thêm startupProbe khoảng180s, timeout readiness5s/liveness3s, giữ nguyên CPU/RAM/replicas. Không đổi cấu trúc data/model/schema.
- Core suite local: **422 ca chạy, 421 đạt/1 skip do môi trường TensorFlow**, không failure/error. Full CI Linux **42039874320** có TensorFlow đạt **427/427, không skip**, cùng kiểm import maintenance/application trong image.
- Vite build đạt. Browser với API giả kiểm desktop/mobile, cloud không cấu hình và provider trả lỗi: không switch, đủ năm lựa chọn, payload chỉ Kaggle, không có local fallback; không gửi request thật tới backend/provider.
- Source ứng viên tạm với commit placeholder (không phải artifact phát hành) đã qua startup tree/neural/API, sidecar import, gate và release health; API không có executor routes/import. Airflow import/graph/skipped receipt/local-dispatch checks và server dry-run đạt. Phải kiểm lại artifact/digest thật sau CI; không dùng candidate local để Apply.
- Bằng chứng trong `results/cicd/`: `kaggle-only-core-tests-v2.log`, `kaggle-only-browser-proof.json`, screenshot desktop/mobile, `api-optimization-startup-v2.json`, các log CI và Windows Plan. Artifact thật đã được đối chiếu nguồn/hash với snapshot đã kiểm trước khi triển khai.

## 9. Kết quả phát hành backend — 05/11/2026

- [Deploy 42040407576](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/runs/42040407576) thành công sau người dùng duyệt. Backend commit `3cbd0fe`, release `1996277cfb7a6fa6dbe113dec0ee2407a8d8a52901fc9e5be351dbeda227e4a9`; receipt completed=true, guard_held=false, recovery_required=false. API2, worker cây/neural và Airflow2 đều Verify đạt, không restart ở mốc kiểm sau rollout.
- GET trực tiếp trên cả hai API xác nhận readiness ready, OpenAPI không còn executor training, backend mới chỉ Kaggle và mặc định Kaggle. DAG đã về unpaused, không có lượt controlled/Airflow đang hoạt động. Không đổi model/dataset hoặc chạy training để thử.
- Hai sidecar mỗi bên upload141 file một lần đầu; các vòng sau uploaded0/unchanged141. Đã thấy cơ chế bỏ qua file không đổi hoạt động thật; chưa coi đây là benchmark giảm CPU/RAM dài hạn.
- Frontend bỏ công tắc được phát hành thành gói riêng sau backend Verify; theo dõi kết quả Pages và bản đang phục vụ trong handoff. Mô hình/lịch sử local cũ vẫn được giữ để đọc và phục vụ dự báo.
