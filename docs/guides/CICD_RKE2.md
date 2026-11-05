# Vận hành CI/CD backend trên RKE2

Phương án đã được người dùng chốt ngày 05/11/2026: **CI kiểm chứng → duyệt một lần trên GitHub → runner Windows triển khai qua Tailscale → Verify**. Thiết lập ban đầu và nghiệm thu live vẫn là các bước riêng, không suy ra đã hoàn thành từ việc có workflow trong source.

## Thành phần

| File | Vai trò |
| --- | --- |
| `.github/workflows/backend-ci.yml` | PR/push: test backend/ML, dựng gói source theo danh sách cho phép; push nhánh mặc định đạt test mới build/publish image GHCR. |
| `.github/workflows/backend-deploy.yml` | Xác minh CI đúng repo/nhánh/commit và cổng duyệt thật, chờ `rke2-production`, giao job cho runner `rke2-deploy`. |
| `.github/workflows/frontend-ci.yml` | Build frontend trong PR/push; workflow Pages hiện có tiếp tục phát hành frontend. |
| `scripts/cicd/release.py` | Build/Verify gói source, hash từng file, ràng buộc commit/base image/schema. |
| `scripts/cicd/deploy.py` | Plan/Apply/Verify/Rollback; patch có kiểm UID/resourceVersion/spec, không apply manifest nền. |
| `scripts/cicd/maintenance.py` | Giữ khóa PostgreSQL trong Job riêng xuyên suốt thay pod để chặn yêu cầu công việc mới lọt giữa bước kiểm idle và rollout. |
| `scripts/cicd/setup.ps1` | Thiết lập một lần theo các action tường minh; mặc định Check chỉ đọc. |
| `deployment/rke2/` | Baseline không chứa bí mật, RBAC và ba file runtime/DAG được theo dõi trong Git. |

## Gói phát hành giai đoạn đầu

Image kế thừa **đúng digest v34 hiện đã dùng**, không cài lại/nâng dependency. Source mới được đặt trong `/opt/traffic-release`; init container sao chép vào volume riêng cho mỗi pod trước khi ứng dụng chạy. Các lớp compatibility/SHAP/restore đang được mount trên cụm được giữ nguyên, kể cả các kiểm SHA gốc. Đây là cơ chế nâng cấp cụm hiện có; chưa phải công cụ dựng một cụm mới hoàn toàn từ con số không.

Entrypoint vẫn thực hiện serving gate và training control. Thêm health release để đối chiếu commit, release ID, source bundle và base image thực chạy. Hai Airflow chỉ cập nhật khi hash DAG/control thay đổi. PostgreSQL, MinIO, MLflow, volume dữ liệu, model active và dataset không nằm trong phạm vi cập nhật.

`baseline.json` khóa hash requirements và các migration đang có. Nếu thay dependency hoặc schema, build phải dừng để chuẩn bị nâng cấp tương thích riêng; pipeline này không tự chạy migration chưa được review. Không dùng việc sửa hash baseline như một cách bỏ qua việc kiểm tương thích.

## Thiết lập một lần trên máy Windows hiện tại

Chạy từ root `main_project`. Các action ngoài Check có thay đổi rõ ràng; chỉ chạy action cần thiết. Các file runner/token nằm trong `results/cicd/` đã bị ignore, không đưa vào Git hoặc upload làm bằng chứng.

```powershell
./scripts/cicd/setup.ps1 -Action Check
./scripts/cicd/setup.ps1 -Action ConfigureGitHub
```

ConfigureGitHub tạo cổng duyệt cho tài khoản GitHub đang đăng nhập, cho phép chính người tạo lượt chạy duyệt release của mình, giới hạn nhánh mặc định và tắt bypass admin. Các biến `RKE2_CONTEXT`, `RKE2_NAMESPACE_UID`, `RKE2_KUBECONFIG` được cấu hình; **RKE2_CD_ENABLED vẫn false**. Script dùng credential Git đã có, không yêu cầu dán token vào chat.

Bước quyền Kubernetes do chủ cụm thực hiện theo quy tắc vận hành hiện hành:

```powershell
./scripts/cicd/setup.ps1 -Action CreateAccess
```

Action này apply **chỉ** `deployment/rke2/delivery-rbac.yaml`, tạo ServiceAccount `traffic-delivery`, quyền patch đúng năm Deployment ứng dụng, quyền đọc/exec pod và tạo Job bảo trì trong namespace. Không cấp quyền đọc Secret, patch dịch vụ dữ liệu hoặc node proxy. Quyền exec/tạo Job vẫn có khả năng truy cập ứng dụng trong namespace; chỉ dùng credential này cho job phát hành đã duyệt.

Sau đó action lấy TokenRequest có thời hạn và ghi kubeconfig riêng với ACL cho tài khoản Windows hiện tại. Không sao chép user/key quản trị từ kubeconfig cá nhân. Thời hạn token có thể bị API server giới hạn; cần theo dõi và cấp lại riêng trước khi hết hạn. Chưa có cơ chế tự gia hạn token trong bản đầu.

```powershell
./scripts/cicd/setup.ps1 -Action RegisterRunner
./scripts/cicd/setup.ps1 -Action StartRunner
```

RegisterRunner tải Windows x64 runner chính thức, kiểm checksum, đăng ký label `rke2-deploy`, không tự chạy. StartRunner chạy nền ẩn dưới tài khoản Windows hiện tại, dùng `_work` riêng bên trong thư mục runner. Source đang mở không bị reset/clean. Khi cần, runner Node dùng bundle các CA public Windows đã tin cậy; không tắt xác minh TLS.

Máy cần bật, Tailscale và runner hoạt động khi deploy. Bản đầu chưa cài service/Task Scheduler tự khởi động sau đăng nhập hoặc reboot; chạy StartRunner lại khi mở phiên Windows mới. GitHub có thể giữ job chờ khi runner offline.

Repo hiện là public: chỉ giao job phát hành từ nhánh tin cậy cho runner có quyền cụm; không chạy mã PR chưa tin cậy trên máy này. Cổng duyệt bảo vệ workflow deploy đã cấu hình, không biến máy Windows thành môi trường cách ly cho mọi workflow khác trong repo.

## Publish source, image và bật CD

1. Review/commit/push source theo quyền Git của nhiệm vụ; không force-add runtime lịch sử, results hoặc nhật ký. CI cần file runtime mới được theo dõi trong Git.
2. Xem lượt **Backend CI** đầu tiên; chỉ tiếp tục khi các job bắt buộc đạt và có artifact `backend-release-<commit>` cùng image digest GHCR. Máy local không có TensorFlow không thay bằng chứng job neural trên GitHub.
3. Xác nhận node pull được image. Package GHCR mới có thể private. Với image chứa source công khai đã review, chủ repo có thể chọn public trong Package Settings; nếu giữ private, cấu hình pull Secret có credential đọc package phù hợp. Không dùng GITHUB_TOKEN hết hạn của một job làm credential pull lâu dài.
4. Bật CD bằng **một** trong hai cách theo lựa chọn registry:

```powershell
# Chỉ dùng sau khi đã xác nhận image có thể pull ẩn danh:
./scripts/cicd/setup.ps1 -Action EnableCD -PublicImageConfirmed

# Hoặc dùng Secret pull riêng đã được chủ cụm cấu hình:
./scripts/cicd/setup.ps1 -Action EnableCD -PullSecretName <ten-secret-da-review>
```

EnableCD kiểm reviewer, runner online và đúng namespace UID trước khi đặt biến true; lựa chọn registry vẫn cần nghiệm thu pull thật. Không tự đổi package sang public hoặc tự tạo Secret từ token cá nhân.

Lượt CI đã hoàn tất lúc CD còn false sẽ không tự chạy lại deploy khi bật biến. Để phát hành bản đó: GitHub → Actions → Deploy Backend RKE2 → Run workflow → nhập ID lượt Backend CI **push nhánh mặc định** đã đạt. Sau đó duyệt môi trường như bình thường.

## Mỗi lần phát hành

Push vào nhánh mặc định → Backend CI tạo image/artifact → Deploy Backend RKE2 kiểm provenance → chờ **Review deployments → rke2-production → Approve and deploy**. Chỉ cần một lần duyệt cho toàn bộ job, không xác nhận từng lệnh kubectl.

Runner xác minh artifact đúng commit và image digest. Preflight kiểm namespace UID, trạng thái training, migration head, cache API 4Gi, dung lượng restore dưới 80%, disk headroom và entrypoint. Runner giữ khóa bảo trì từ Job độc lập, pause DAG theo trạng thái đã ghi rồi cập nhật hai worker/API. Trong cửa sổ giữ khóa, các request ứng dụng chịu barrier có thể tạm trả busy; không cam kết zero downtime. Auth/health và một số route đọc vùng không bị barrier này chặn.

Verify kiểm mọi replica đang hoạt động, release thực chạy, API readiness và import/graph/hash DAG; pod Failed/Completed lịch sử không tính là replica ready. Cuối cùng khôi phục pause state, xác nhận fingerprint active model/dataset không đổi và thả khóa. Chạy lại đúng release đã có chỉ Verify, không tạo rollout hoặc Job bảo trì mới.

## Lỗi, log và phục hồi

- Test không đạt: không publish release production và không giao job lên máy local.
- Chưa có reviewer thật hoặc CD false: không có job triển khai được chạy.
- Preflight không đạt: dừng trước mutation.
- Rollout/health lỗi khi guard còn giữ và spec chưa bị người khác đổi: hoàn tác patch đã thực hiện, kiểm spec/rollout, khôi phục pause state và thả khóa. Không tự rollback schema/data.
- Mất guard, job bị hủy hoặc có thay đổi ngoài quy trình: receipt ghi `recovery_required`; không tự ghi đè trạng thái ngoài dự kiến. Giữ pause state để xem xét phục hồi.

Receipt sanitized trong artifact `delivery-receipt-...`, có source/image identity, UID/spec hash và patch ngược chỉ gồm tham chiếu; không có raw Deployment, env hoặc Secret. Không upload kubeconfig, `.credentials`, log đăng ký runner hoặc private source snapshot.

Guard Job có deadline một giờ và TTL sau kết thúc. Nếu runner mất kết nối giữa chừng, không giả định DAG tự resume: đọc receipt và kiểm Job/pod/pause state trước khi tiếp tục. Khi guard vẫn còn giữ và trạng thái phù hợp, chủ vận hành dùng Rollback với **đúng journal/artifact/image/commit**; không dùng journal của release khác. Nếu guard đã mất thì cần lập lại trạng thái phục hồi từ hiện trạng, không replay Apply cũ.

## Kiểm chứng

Kế hoạch ca thử chi tiết ở mục 6 của [đề xuất](CICD_RKE2_PROPOSAL.md). Kiểm lỗi có chủ đích/rollback end-to-end trong môi trường thử tách dữ liệu trước; lần nghiệm thu production dùng một bản hợp lệ đã được duyệt.

Đã kiểm local trong đợt tích hợp: unit test điều khiển/đóng gói/cổng duyệt, core regression trên checkout riêng, build frontend, actionlint, parser PowerShell và server dry-run. Chưa có bằng chứng build/pull/startup của image phát hành hoặc một lượt deploy qua Actions cho đến khi source được publish, CI đạt và người dùng duyệt. Không đánh dấu phần này hoàn tất chỉ từ test mock.
