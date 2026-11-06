# Vận hành CI/CD backend trên RKE2

Phương án đã được người dùng chốt ngày 05/11/2026: **CI kiểm chứng → duyệt một lần trên GitHub → runner Windows triển khai qua Tailscale → Verify**. Thiết lập ban đầu và nghiệm thu live vẫn là các bước riêng, không suy ra đã hoàn thành từ việc có workflow trong source.

Bản nâng cấp 06/11 bổ sung lựa chọn Manual/Auto có trạng thái lưu lâu dài;
đường rollout/guard/Verify vẫn giữ. Xem [CD Control](CD_MODES.md) cho cách đổi
chế độ, chốt lỗi, bằng chứng trước rollback và xác nhận phục hồi. Mốc nghiệm
thu live của bản nâng cấp phải tra handoff; tài liệu source không thay bằng chứng.

## Thành phần

| File | Vai trò |
| --- | --- |
| `.github/workflows/backend-ci.yml` | PR/push: test backend/ML, dựng gói source theo danh sách cho phép; push nhánh mặc định đạt test mới build/publish image GHCR. |
| `.github/workflows/backend-deploy.yml` | Xác minh CI đúng repo/nhánh/commit và cổng duyệt thật, chờ `rke2-production`, giao job cho runner `rke2-deploy`. |
| `.github/workflows/frontend-ci.yml` | Build frontend trong PR/push; workflow Pages hiện có tiếp tục phát hành frontend. |
| `scripts/cicd/release.py` | Build/Verify gói source, hash từng file, ràng buộc commit/base image/schema. |
| `scripts/cicd/deploy.py` | Plan/Apply/Verify/Rollback; patch có kiểm UID/resourceVersion/spec, không apply manifest nền. |
| `scripts/cicd/maintenance.py` | Giữ khóa PostgreSQL trong Job riêng xuyên suốt thay pod để chặn yêu cầu công việc mới lọt giữa bước kiểm idle và rollout. |
| `scripts/cicd/cache_sync.py` | Nạp uploader của release sau kiểm hash và compatibility contract; sidecar dùng cùng image digest với release. |
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

RegisterRunner tải Windows x64 runner chính thức, kiểm checksum, đăng ký label `rke2-deploy`, không tự chạy. Work directory đặt ngắn tại `D:\traffic-cd-work` trên máy dự án hiện tại (tổng quát: `traffic-cd-work` ở root ổ chứa dự án), tách khỏi thư mục cài runner và source đang mở. GitHub còn lặp tên repo hai lần bên dưới work directory, nên đặt work trong đường dẫn dự án dài có thể khiến Git lỗi ngay khi tạo pack file.

StartRunner chạy nền ẩn dưới tài khoản Windows hiện tại và bật `core.longpaths=true` qua biến môi trường chỉ cho tiến trình runner/con của nó. Không sửa Git config global/system hoặc Windows registry. Source đang mở không bị reset/clean. Khi cần, runner Node dùng bundle các CA public Windows đã tin cậy; không tắt xác minh TLS.

Với runner cũ còn dùng `_work` sâu trong dự án và bị `Filename too long`, chỉ khi runner idle và không có Worker đang chạy:

```powershell
./scripts/cicd/setup.ps1 -Action RepairRunner
./scripts/cicd/setup.ps1 -Action StartRunner
```

RepairRunner dùng công cụ cấu hình runner chính thức để đăng ký lại cùng repo với work directory ngắn, giữ nguyên checkout lỗi cũ và workspace dự án. Nó từ chối nếu runner đang bận, đã dùng work directory ngắn hoặc đích mới không rỗng; không chạy lại tùy tiện trên runner đã được sửa.

Máy cần bật, Tailscale và runner hoạt động khi deploy. Bản đầu chưa cài service/Task Scheduler tự khởi động sau đăng nhập hoặc reboot; chạy StartRunner lại khi mở phiên Windows mới. GitHub có thể giữ job chờ khi runner offline.

Repo hiện là public: chỉ giao job phát hành từ nhánh tin cậy cho runner có quyền cụm; không chạy mã PR chưa tin cậy trên máy này. Cổng duyệt bảo vệ workflow deploy đã cấu hình, không biến máy Windows thành môi trường cách ly cho mọi workflow khác trong repo.

Khi dựng lại, kiểm Settings → Actions để workflow từ **mọi cộng tác viên ngoài repo** phải được maintainer duyệt (`all_external_contributors`), kể cả người đã từng có PR được merge. Thiết lập này đã được cấu hình trong đợt kích hoạt; không ảnh hưởng luồng push/duyệt release của chủ repo.

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

Script Windows chọn **một** executable Python/kubectl theo thứ tự PATH; `Get-Command` có thể trả nhiều Application và không được truyền cả mảng vào toán tử `&`. Khi đã nạp các biến RKE2_* cần thiết, có thể kiểm chính wrapper bằng `deliver.ps1 -ArtifactDirectory <artifact> -ExpectedCommit <sha> -Action Plan`: kiểm file và server dry-run, không Apply. Workflow thông thường giữ mặc định Apply, sau đó tự Verify.

Verify kiểm mọi replica đang hoạt động, release thực chạy, API readiness và import/graph/hash DAG; pod Failed/Completed lịch sử không tính là replica ready. Cuối cùng khôi phục pause state, xác nhận fingerprint active model/dataset không đổi và thả khóa. Chạy lại đúng release đã có chỉ Verify, không tạo rollout hoặc Job bảo trì mới.

## Lỗi, log và phục hồi

Gói tối ưu API/Kaggle-only bổ sung startup probe API, timeout readiness5s/liveness3s và cập nhật uploader tăng dần trong **đúng sidecar**, có inverse patch. Nguồn uploader được bind vào release metadata và kiểm khi Verify. Không giảm resource/replicas hoặc tự bỏ guard bảo trì. Startup preflight kiểm API không còn route/import executor training, thử import sidecar mới trên từng API, và kiểm DAG Kaggle-only/receipt trong tiến trình Airflow riêng trước mutation.

Uploader chỉ bỏ qua file đã upload thành công và không đổi; đối soát mặc định mỗi giờ, có retry và vẫn tôn trọng tombstone/barrier. Bộ nhớ theo dõi chỉ nằm trong sidecar, restart sẽ upload lại để thiết lập trạng thái; không xóa artifact trên MinIO. Env tùy chọn `TRAFFIC_API_ARTIFACT_RECONCILE_SECONDS` tối thiểu300s, mặc định3600s; chu kỳ nghỉ upload vẫn theo `TRAFFIC_API_ARTIFACT_SYNC_SECONDS` hiện hữu.

Image build chạy `maintenance.py --check-imports` trong chính base image trước khi publish. Guard nạp ứng dụng từ `/app` rõ ràng dù script nằm ở `/opt/traffic-release`; phép thử import không đọc cấu hình runtime hay mở kết nối DB. Nếu guard dừng lúc khởi động, controller đọc trạng thái kết thúc và chỉ ghi phase/loại lỗi đã lọc vào receipt, tránh lỗi exec che mất nguyên nhân.

Image build còn kiểm graph import control/monitor với source đã đóng gói. Trước tạo Job bảo trì hoặc pause DAG, Apply khởi tạo chính source ứng viên trong tiến trình riêng trên từng pod API/cây/neural hiện hữu, kiểm route, release identity và token/serving gate. Phép thử không khởi chạy app lifespan, bootstrap user hoặc training. Nếu không trả proof đủ, dừng trước rollout. Plan vẫn chỉ kiểm package/server dry-run; không thay cho kiểm khởi tạo này. Khi cập nhật lần sau, checker nhận metadata ứng viên riêng để không nhầm với release đang chạy.

Package cha mới được nạp bằng finder dự phòng sau các loader của image/lớp tương thích; package cũ và kiểm hash vẫn giữ ưu tiên. API dùng các quality/feature routes trong source mới cùng serving guards đã kiểm; không gọi lớp thêm route cũ trên API đã có các route đó.

- Test không đạt: không publish release production và không giao job lên máy local.
- Chưa có reviewer thật hoặc CD false: không có job triển khai được chạy.
- Preflight không đạt: dừng trước mutation.
- Rollout/health lỗi khi guard còn giữ và spec chưa bị người khác đổi: hoàn tác patch đã thực hiện, kiểm spec/rollout, khôi phục pause state và thả khóa. Không tự rollback schema/data.
- Mất guard, job bị hủy hoặc có thay đổi ngoài quy trình: receipt ghi `recovery_required`; không tự ghi đè trạng thái ngoài dự kiến. Giữ pause state để xem xét phục hồi.

Receipt sanitized trong artifact `delivery-receipt-...`, có source/image identity, UID/spec hash và patch ngược chỉ gồm tham chiếu; không có raw Deployment, env hoặc Secret. Không upload kubeconfig, `.credentials`, log đăng ký runner hoặc private source snapshot.

Guard Job có deadline một giờ và TTL sau kết thúc. Nếu runner mất kết nối giữa chừng, không giả định DAG tự resume: đọc receipt và kiểm Job/pod/pause state trước khi tiếp tục. Khi guard vẫn còn giữ và trạng thái phù hợp, chủ vận hành dùng Rollback với **đúng journal/artifact/image/commit**; không dùng journal của release khác. Nếu guard đã mất thì cần lập lại trạng thái phục hồi từ hiện trạng, không replay Apply cũ.

## Kiểm chứng

Kế hoạch ca thử chi tiết ở mục 6 của [đề xuất](CICD_RKE2_PROPOSAL.md). Kiểm lỗi có chủ đích/rollback end-to-end trong môi trường thử tách dữ liệu trước; lần nghiệm thu production dùng một bản hợp lệ đã được duyệt.

Đã kiểm local trong đợt tích hợp: unit test điều khiển/đóng gói/cổng duyệt, core regression trên checkout riêng, build frontend, actionlint, parser PowerShell và server dry-run.

Mốc kích hoạt 05/11/2026: [Backend CI 42009647009](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/runs/42009647009) đạt **414/414 test**, không skip, cùng hai phép kiểm import trong image. Sau user duyệt, [release 42010515273](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/runs/42010515273) **success**: startup preflight đủ ba role, rollout API/hai worker, health/identity/DAG và Verify đều đạt; Airflow giữ nguyên. Receipt xác nhận `completed=true`, `guard_held=false`, `recovery_required=false`; user xác nhận thành công. CD đã bật và mỗi release vẫn cần duyệt trên GitHub.

Trước đó [lượt 42006213213](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/runs/42006213213) lỗi package khi khởi động worker; runner đã tự hoàn tác worker, khôi phục DAG và nhả guard. Receipt xác nhận `rolled_back=true`, `recovery_required=false`. Đây là bằng chứng rollback thật của lượt đó; chưa phải nghiệm thu mọi kịch bản no-op, concurrency hoặc recovery ở staging. Chi tiết trong [handoff](../../My-worklog/hand-offs/2026-11-05-system-optimization.md).

Không đánh dấu rollout đạt chỉ từ CI/mock/server dry-run; đối chiếu receipt và ứng dụng sau triển khai. Kết quả trên là bằng chứng của các release cụ thể, không thay kiểm tương thích cho dependency/schema mới hoặc cho phép tự chạy training/Activate trong lần phát hành sau.
