# Khai báo thành phần và phát hành nhanh

`deployment/cicd/components.json` là danh sách image được hỗ trợ. Workflow
đọc danh sách từ gói nguồn đã kiểm, sinh matrix và dùng chung bước build/check.
Mỗi thành phần có tên, loại, entrypoints/resources, dependency và cách build.
Không thêm job YAML chỉ để thêm một execution image theo mẫu hiện có.

## Các loại được hỗ trợ

| Loại | Cách vận hành |
| --- | --- |
| api/worker/sidecar | Bốn image hiện hữu; giữ hợp đồng role, blue/green, cache và retirement. |
| runtime-job-image | Image thực thi theo nhu cầu; worker tạo Job khi admin yêu cầu. CI/CD không tạo training Job mỗi lần push. |

Core API/tree/neural/sync phải tồn tại. Đổi topology, thêm loại dịch vụ khác,
database/PVC hoặc quyền cluster cần bổ sung mẫu và kiểm lifecycle riêng;
không gắn một nhãn bất kỳ để bỏ kiểm. Registry không tự prune tài nguyên.

Để thêm runtime-job-image, khai báo id mới và profile cùng các trường như
local-training, thêm Dockerfile/dependency/checker phù hợp. Executor builder
chỉ nhận đầu vào trong các gốc build đã hỗ trợ: infra/src/backend/deployment/
scripts/requirements; file dependency bắt đầu trong requirements/. Điều này
giữ kiểm source mới nhất đầy đủ khi push nhiều lần; layout khác bị báo lỗi
sớm thay vì bị bỏ sót. Executor builder
đóng gói import closure/resources và tất cả requirement include/constraint
đệ quy vào context riêng. Entry phải dùng base image pin digest. Consumer mới
cần được nối với cấu hình runtime tương ứng; thêm image không tự đổi app.

## Nhận diện thay đổi và chứng cứ

- Source ID không gắn với toàn commit: hash đúng code/dependency/recipe/check
  của từng image. Sửa frontend/API không liên quan không đổi execution image.
- Có image cùng ID đã đạt kiểm thì dùng lại digest từ registry. Executor reuse
  không pull image ML, không build và không lặp kiểm artifact nặng. Check
  script thay đổi cũng đổi ID nên phải kiểm trước khi publish lại.
- Image mới chạy offline check import/thư viện và load/inference trên fixture
  năm model; không fit, gọi Kaggle hoặc kết nối production.
- Package và acceptance yêu cầu đủ binding đúng registry/SHA/run/attempt.
  `image-*` của từng thành phần được tổng hợp riêng, không ghi đè digest bằng
  output cuối của một matrix. Thiếu/duplicate/sai binding sẽ bị từ chối.
- Reader giữ releasev2 và scopev1–v3. Releasev3/scopev4 thêm inventory và executor
  binding; không viết lại hoặc diễn giải proof lịch sử theo danh sách mới.

Push vào nhánh chính luôn được scope chọn việc bên trong workflow, tránh bỏ
sót Dockerfile nằm ở thư mục mới. Diff vẫn tích lũy từ backend đã Verify.
Docs/frontend không liên quan không chọn build backend. Unknown impact giữ
fast checks đầy đủ; không tự dựng kind. Full chỉ manual/lịch tuần như trước.

## Local: tích hợp an toàn theo hai bước

`deployment/cicd/local-training.json` ban đầu có `enabled=false`. Release gắn
image digest/config vào API/hai worker nhưng không mở token dispatcher ở bước
này. Dịch vụ đang có tiếp tục hoạt động và UI vẫn khóa Local.

Chủ cụm review `deployment/rke2/local-training-rbac.yaml` rồi dùng context
quản trị riêng để bootstrap một lần. Không dùng credential delivery hoặc đưa
credential quản trị vào app. Helper mặc định chỉ Plan/dry-run:

```powershell
.\scripts\cicd\bootstrap_local_training.ps1 -Action Plan `
  -Kubeconfig '<owner-admin-kubeconfig>' -Context '<owner-admin-context>' `
  -NamespaceUid '<traffic-mlops-namespace-uid>'
```

Sau khi review đúng file và SHA256 Plan in ra, chủ cụm thực hiện:

```powershell
.\scripts\cicd\bootstrap_local_training.ps1 -Action Apply `
  -Kubeconfig '<owner-admin-kubeconfig>' -Context '<owner-admin-context>' `
  -NamespaceUid '<traffic-mlops-namespace-uid>' -ReviewedSha256 '<reviewed-sha256>'
```

Helper kiểm namespace UID, server dry-run và hash đã review; chỉ apply file
bootstrap cố định. Không bật Local, tạo Job, train, Activate hoặc sửa CD state.

Sau bootstrap, kiểm metrics/storage/node pressure và image đã kiểm, đổi
`enabled=true` bằng PR/release bình thường. Renderer chỉ cấp dispatcher SA/token
cho tree/neural; API và Job thực thi vẫn không mount token Kubernetes. Job SA
riêng không có quyền gọi Kubernetes. Cả hai worker kiểm read access và quyền
create/delete Job, đọc log; API chỉ mở Local nếu hai worker Ready cùng digest.
Config enable đổi deployment ID, không cần build lại image chỉ vì đổi flag.

CD giữ active-training fence và kiểm thêm Job/Pod Local Pending/terminating
hoặc chưa terminal, kể cả DB không còn phản ánh chúng. Không rollout controller
khi còn công việc chưa đối soát. Rollback khôi phục thay đổi thuộc transaction,
không tự undo Job/data/model hoặc xóa một training đang chạy để deploy tiếp.

## Thời gian và nghiệm thu

Không dựng kind hoặc train thật năm model trong mỗi push. Build dependency ML
đầu tiên/cold cache có thể lâu; các lượt thường dùng lại content digest và cache
riêng của executor. Metrics ghi cả build/reuse của executor. Chỉ so thời gian
trước/sau trên cùng loại thay đổi; CI54 no-op không là SLA cho build mới.

Nghiệm thu Local thật do chủ hệ thống thực hiện sau tích hợp: một cây, từng
deep, cây+deep rồi hai deep nếu đủ budget, đủ năm Local và đủ năm Kaggle.
Theo dõi CPU/RAM/disk, queue/Stop/Retry, output/candidate/SHAP và website.
Không đổi CV/epochs/SHAP để ép vừa RAM. Retrain vẫn luôn Kaggle.

Nguồn thiết kế: [GitHub matrix](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations),
[artifact dependencies](https://skaffold.dev/docs/tutorials/artifact-dependencies/),
[Docker cache](https://docs.docker.com/build/cache/optimize/),
[Google microservices-demo](https://github.com/GoogleCloudPlatform/microservices-demo/blob/main/skaffold.yaml).
Các nguồn là mẫu tham khảo, không chứng nhận tốc độ hoặc zero downtime của dự án.
