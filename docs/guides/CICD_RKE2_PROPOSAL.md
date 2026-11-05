# Đề xuất CI/CD cho frontend và backend RKE2

Ngày khảo sát: 05/11/2026, Asia/Saigon. Phương án đã được người dùng chốt; phần tích hợp và trạng thái vận hành xem [CICD_RKE2.md](CICD_RKE2.md). Nội dung bên dưới giữ bối cảnh/tiêu chí của đề xuất ban đầu, không phải bằng chứng mọi bước đã triển khai.
Phạm vi khảo sát: source local ở HEAD `b0faaf7`; chưa kiểm GitHub settings hoặc cụm đang chạy.

## 1. CI/CD có nghĩa gì trong dự án này?

- **CI — Continuous Integration:** tích hợp thay đổi thường xuyên, tự động build và kiểm thử để phát hiện lỗi.
- **Continuous Delivery:** kiểm tra và đóng gói bản sẵn sàng phát hành; vẫn có thể giữ quyết định đưa lên production cho người vận hành.
- **Continuous Deployment:** thay đổi vượt qua các bước kiểm tra sẽ tự lên production, không cần người duyệt từng bản.

Vì vậy, hiểu CI/CD là tự động hóa triển khai là đúng một phần: quy trình còn bao gồm kiểm tra, đóng gói và chuẩn bị phát hành. CD có bước duyệt vẫn là CI/CD. Xem [định nghĩa CI/CD của AWS](https://docs.aws.amazon.com/whitepapers/latest/practicing-continuous-integration-continuous-delivery/what-is-continuous-integration-and-continuous-deliverydeployment.html).

RKE2 là nơi ứng dụng chạy; GitHub Actions là công cụ có thể điều phối build/test/deploy. Hai thành phần bổ sung cho nhau. GitHub hỗ trợ cả runner do GitHub cung cấp và runner tự quản lý: [GitHub Actions](https://docs.github.com/en/actions/get-started/understand-github-actions).

## 2. Hiện trạng đã đối chiếu từ source

| Thành phần | Bằng chứng | Nhận định |
| --- | --- | --- |
| Frontend | `.github/workflows/deploy-frontend-pages.yml`: push main/master có thay đổi frontend/workflow → npm ci → Vite build → Pages deploy; có workflow_dispatch | Đã có pipeline build/deploy tự động, chưa có job test riêng hoặc PR CI trong workflow này. Không phải mọi push đều deploy. |
| Backend/API/worker | Chỉ có một workflow Pages trong `.github/workflows/`; script rollout riêng cho RKE2 | Source hiện có chưa có pipeline CI/CD backend trên GitHub Actions. Kết quả vận hành thực tế vẫn phải đối chiếu riêng. |
| Runtime/DAG | `scripts/cloud_training/prepare_rollout.py` đọc ba file từ `Deployment_test_on_rke2/runtime/`; thư mục này bị Git ignore | Clone Git chưa đủ source để tái tạo toàn bộ bản phát hành. `tests/test_cloud_rollout.py` cũng dùng runtime local. |
| Cách nạp code | Runtime cloud cài source overlay từ ConfigMap; wrapper dự báo có kiểm SHA và patch cache/gate | Chỉ build image mới rồi đổi image chưa bảo đảm code mới được dùng hoặc startup tương thích. |
| Phụ thuộc | `requirements/backend.txt` chỉ cố định một phần phiên bản | Cần ghi nhận bộ dependency thực tế rồi khóa phiên bản phù hợp để build có thể lặp lại. |
| Docker context | Dockerfile có `COPY . .`; `.dockerignore` chưa loại rõ `.env*`, nhật ký, bộ triển khai và snapshot private | Phải dùng danh sách source được phép đóng gói hoặc build context đã lọc trước khi publish image; Git ignore không thay Docker ignore. Chưa build hoặc publish để kiểm tra nội dung image. |
| Kiểm sức khỏe | `/api/v1/health/live`, `/api/v1/health/ready` có sẵn | Ready kiểm DB/MinIO/MLflow, chưa đủ chứng minh worker/DAG/SHAP/model serving hoạt động. |

Không dùng manifest nền làm hiện trạng production. Ví dụ source `k8s/base/traffic-api.yaml` còn replicas=1, bản triển khai lịch sử có cache 2Gi, trong khi hồ sơ hiện trạng ghi hai API/cache 4Gi. Chưa kiểm lại thông số live trong lượt này.

## 3. Hướng tích hợp khuyến nghị

**GitHub Actions + GHCR + triển khai có chủ đích qua runner nội bộ**, triển khai theo từng giai đoạn.

```text
Pull request ──→ CI: test backend/ML, build frontend, kiểm gói runtime
                        ↓ đạt
Merge master ─→ CI cùng commit → build release → GHCR + release manifest
                                                    ↓
                         Người vận hành chọn bản phát hành đã đạt
                                                    ↓
                         Runner nội bộ: Preflight → Deploy → Verify
                                                    ↓
                                       Cụm RKE2 hiện có
```

Frontend tiếp tục dùng Pages. Bổ sung CI trước khi merge; khi có thay đổi hợp đồng API, bản phát hành cần quy định thứ tự backend tương thích trước rồi frontend. Các sửa UI độc lập giữ cách phát hành hiện có.

GHCR là kho image gắn với GitHub; workflow có thể publish bằng `GITHUB_TOKEN` với quyền phù hợp. Không mặc định package sẽ public; chọn visibility và quyền pull lúc cấu hình. Xem [GitHub Container Registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

### Giai đoạn 1 — Chuẩn hóa để dựng lại từ Git

1. Kiểm kê chuỗi runtime API, worker cây, worker neural và DAG đang dùng; cần đối chiếu chỉ đọc trên cụm khi bắt đầu triển khai nhiệm vụ.
2. Đưa các source runtime cần thiết vào vị trí được Git theo dõi, ví dụ `deployment/rke2/runtime/`, và cập nhật tham chiếu/test. Chỉ đưa source đã rà soát; review, snapshot, Secret, dữ liệu và bằng chứng riêng vẫn nằm ngoài Git.
3. Chuẩn hóa entrypoint theo vai trò, giữ serving gate, restore/cache, chọn backend, SHAP, receipt và kiểm toàn bộ nhánh trước finalize. Không thay entrypoint đang chạy bằng `uvicorn app:app` đơn thuần.
4. Chuẩn bị bộ dependency tương thích với model/preprocessor hiện có; khóa phiên bản và kiểm reload model. Không tự nâng TensorFlow/SHAP/sklearn chỉ để có CI.
5. Chuẩn hóa build context và bộ phát hành không phụ thuộc `results/` hay journal rollout lịch sử. Gói phát hành ghi commit SHA, digest image, hash runtime/DAG và migration revision.

**Điều kiện đạt:** checkout sạch dựng/kiểm được các vai trò từ source theo dõi bởi Git, không cần copy thêm file private từ máy phát triển.

### Giai đoạn 2 — Thêm CI cho toàn ứng dụng

- PR và push nhánh chính chạy kiểm tra phù hợp với phần thay đổi; test backend/ML dùng môi trường riêng, không dùng DB/MinIO/Airflow production hoặc credential Kaggle.
- Nhóm nhanh: API/service, dữ liệu, registry, feature policy, cloud protocol bằng mock; frontend `npm ci` và `npm run build`.
- Nhóm ML/runtime: SHAP thật với fixture nhỏ, import/startup theo vai trò, neural preprocessing/save-reload và DAG import/graph trong dependency tương ứng. Chia job theo bộ phụ thuộc; báo skip rõ, không coi skip TensorFlow là đã kiểm neural đạt.
- Kiểm package/manifest sinh offline; không chạy lại các script rollout theo gói lịch sử chỉ để làm CI xanh.
- Khi CI đã hoạt động ổn, cấu hình required checks trên nhánh chính theo khả năng repo/account. Nếu chưa cấu hình branch protection thì CI chỉ báo kết quả, chưa chặn merge.

**Điều kiện đạt:** PR lỗi bị phát hiện trước merge; checkout sạch chạy được các nhóm bắt buộc mà không dựa vào máy local hoặc production.

### Giai đoạn 3 — Tự động tạo bản phát hành backend

- Chỉ bản từ nhánh chính đã đạt CI mới publish image; PR kiểm build, không phát hành production.
- Gắn image với commit SHA và triển khai theo digest bất biến; không dùng `latest` làm định danh bản production.
- Mục tiêu là source/runtime chuẩn hóa được đóng trong image. Giữ chung image ứng dụng cho API/hai worker lúc đầu nếu tương thích, khác entrypoint theo vai trò; chưa cần tách thêm image để làm đẹp kiến trúc. DAG dùng gói/image tương thích phiên bản Airflow.
- Trong giai đoạn chuyển đổi, release phải ghi rõ cả base image digest và overlay hash; chỉ loại lớp code overlay cũ sau khi kiểm tương đương. Không để image mới âm thầm bị overlay cũ ghi đè.
- Build/test nặng chạy trên GitHub-hosted runner. Dung lượng image, thời gian build, quota và chi phí registry/Actions cần đo từ lần build được giao; chưa có số liệu để hứa mức tiết kiệm.

**Điều kiện đạt:** từ commit xác định được image/runtime/DAG nào đã kiểm; triển khai dùng đúng artifact đó, không build lại trên từng node.

### Giai đoạn 4 — Tự động hóa thao tác triển khai RKE2

- Người dùng xác nhận máy Windows đang mở dự án cũng là máy kubectl: hiện mở PowerShell tại dự án để triển khai qua Tailscale. Ưu tiên xem xét chính máy này làm runner triển khai; chưa cần giả định thêm VM. Build/test nặng vẫn đặt trên GitHub-hosted runner. Việc chọn/cài runner chưa được thực hiện.
- Runner tự kết nối GitHub qua HTTPS; không cần mở Kubernetes API ra Internet chỉ để Actions truy cập. Cần kiểm outbound, ACL và chứng chỉ thực tế. Tham khảo [kết nối self-hosted runner](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).
- Job triển khai chỉ nhận commit/artifact đã kiểm từ nhánh được phép, không nhận mã PR chưa tin cậy; quyền Kubernetes giới hạn đúng phạm vi deploy/check cần thiết. Xem [hướng dẫn bảo mật Actions](https://docs.github.com/en/actions/reference/security/secure-use).
- Giai đoạn đầu người vận hành chọn release bằng `workflow_dispatch`. Nếu repo/plan hỗ trợ, dùng environment `rke2-production` có required reviewers. Required reviewers phụ thuộc visibility/plan; thao tác khởi chạy thủ công không mặc nhiên tương đương cơ chế duyệt độc lập. Xem [deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).
- Node RKE2 cần pull được image. Chọn `imagePullSecrets` cho registry private phù hợp; chỉ sửa cấu hình registry node khi thực sự cần. RKE2 có cơ chế private registry riêng: [RKE2 registry configuration](https://docs.rke2.io/install/private_registry).

**Làm rõ đường mạng GitHub–Tailscale–RKE2:** GitHub điều phối workflow; máy runner mới là nơi chạy `kubectl`. Runner GitHub thông thường chưa tự có đường vào tailnet của dự án. Có hai cách thiết kế:

| Cách | Máy chạy lệnh triển khai | Điều kiện và đánh đổi |
| --- | --- | --- |
| Runner nội bộ — hướng đang đề xuất | Máy/VM quản trị cài Actions runner, có Tailscale và kubectl | Máy chủ động kết nối GitHub qua HTTPS outbound 443 để nhận job, rồi gọi API RKE2 qua Tailscale. Cần máy online khi triển khai và vận hành runner. Không cần GitHub mở kết nối trực tiếp vào máy. |
| Runner GitHub kết nối Tailscale tạm thời | Máy GitHub-hosted cài/kết nối Tailscale trong job triển khai sau bước xác nhận | Dùng Tailscale GitHub Action chính thức và identity dành riêng cho CI, thiết bị ephemeral, tag/ACL phù hợp; kết thúc job ngắt kết nối. Không cần duy trì máy runner riêng, nhưng phải cấu hình cho phép CI tham gia tailnet. |

Nguồn: [GitHub self-hosted runner communication](https://docs.github.com/en/actions/reference/runners/self-hosted-runners), [Tailscale GitHub Action](https://tailscale.com/docs/integrations/github/github-action).

Tailscale chỉ cung cấp đường mạng. Cả hai cách vẫn cần Kubernetes credential/certificate hợp lệ và RBAC đúng phạm vi; không dùng kubeconfig cluster-admin của người dùng như cấu hình mặc định cho CI. ACL phải cho đúng identity tới API endpoint/port thực tế, TLS phải hợp lệ; ngrok đang phục vụ API ứng dụng không thay đường quản trị Kubernetes.

Ứng viên cụ thể là máy Windows phát triển/quản trị hiện tại. Khi thực hiện, cần kiểm từ đúng tài khoản/chế độ chạy runner: PowerShell, đường dẫn công cụ, Tailscale, Kubernetes credential/TLS/RBAC và quyền lưu hồ sơ triển khai. Việc terminal hiện tại dùng được kubectl không tự chứng minh một Windows service dưới tài khoản khác cũng có đủ cấu hình/quyền. Máy cần bật và runner/Tailscale hoạt động khi deploy; mất kết nối hoặc thiếu quyền phải dừng trước mutation.

Runner nên dùng thư mục làm việc riêng, checkout đúng commit hoặc tải đúng artifact đã duyệt; không chạy git reset/clean hay cập nhật đè workspace đang mở trong trình soạn thảo. CI/CD cần tiếp tục giải quyết source ignored/gói phát hành để runner không phụ thuộc vào những file chỉ có trong working tree của người dùng. Lệnh triển khai vẫn chạy trên máy này, các pod/backend vẫn chạy trên RKE2.

Luồng phát hành cần có:

1. **Preflight:** kiểm version/hash hiện tại, thay đổi spec ngoài dự kiến, tài nguyên/cache và model/preprocessor hiện có; chặn khi training đang queued/running/finalizing/cancelling. Có cơ chế khóa/chặn nhận training mới trong khoảng cập nhật, không chỉ kiểm idle một lần.
2. **Deploy:** chuẩn bị diff/dry-run đúng release và phạm vi ứng dụng; migration nếu có phải là bước riêng, chạy một lần với kế hoạch tương thích. Không apply lại toàn bộ manifest nền hoặc cập nhật dịch vụ dữ liệu kèm theo.
3. **Verify:** kiểm rollout từng vai trò, version/hash thực dùng, readiness, serving gate, DAG graph và health worker; kiểm GET forecast-dashboard có xác thực với region thử đã chọn khi phù hợp. Health xanh chưa đủ kết luận model phục vụ tốt.
4. **Kết thúc:** khôi phục trạng thái pause/dispatch trước phiên deploy và ghi receipt chứa commit/digest/kết quả. Một môi trường chỉ có một lượt deploy; không hủy ngang deploy đang chạy vì có push mới.
5. **Lỗi:** ngừng bước tiếp theo và khôi phục release trước khi còn tương thích. Rollback cả runtime/entrypoint/DAG liên quan; `rollout undo` riêng image không hoàn tác DB hoặc artifact. Không tự rollback schema/data theo giả định.

Không có cam kết zero downtime trước khi đo readiness, dung lượng node/cache và chiến lược rollout. CI/CD phần mềm cũng không tự cấp quyền train thật hoặc Activate model mới.

**Điều kiện đạt:** người vận hành chọn đúng release, pipeline cập nhật/kiểm được hệ thống và có đường khôi phục đã kiểm; không phải sửa file/copy source thủ công từng lần.

## 4. Có cần Jenkins hoặc Argo CD ngay không?

| Hướng | Đánh giá cho dự án |
| --- | --- |
| GitHub Actions + runner triển khai nội bộ | Khuyến nghị bước đầu: tái dùng GitHub đang có và kiểm soát được điều kiện idle/rollout nhiều vai trò. Cần vận hành runner và giới hạn quyền. |
| GitHub Actions + Argo CD/GitOps | Hợp lý khi manifest đã biểu diễn đúng trạng thái mong muốn và muốn đối chiếu Git–cluster. Thêm controller, cấu hình và xử lý training guard/migration; để sau bước chuẩn hóa. |
| Jenkins | Cần thêm hệ thống CI phải quản trị; chưa thấy nhu cầu hiện tại mà Actions không đáp ứng. |

Argo CD có thể đồng bộ tự động từ manifest trong Git, giảm nhu cầu để CI gọi trực tiếp API triển khai. Nhưng auto-sync thông thường không tự biết quy tắc tránh gián đoạn training của ứng dụng này; phải thiết kế thêm. Xem [Argo CD automated sync](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/).

## 5. Phạm vi bước tiếp theo và phần chưa xác nhận

Đề xuất bắt đầu **giai đoạn 1 + 2**: chuẩn hóa source/gói phát hành và CI. Sau đó xây image pipeline, rồi tích hợp runner CD sau khi có quyền cấu hình/triển khai cụ thể. Tự động production mỗi lần merge là lựa chọn sau khi các bước trên đã ổn và người dùng chốt thay đổi cách vận hành.

Trước CD cần xác nhận bằng kiểm tra thực tế: visibility/plan GitHub và quyền package, máy runner/đường mạng, digest image/overlay live, dependency thực dùng, dung lượng còn trống, cách khóa dispatch training, trạng thái schema và vùng thử cho smoke test. Đây là đầu vào cần khảo sát, không phải lỗi đã kết luận của hệ thống.

Lượt lập đề xuất này chỉ đọc source/tài liệu và nguồn chính thức; chưa thêm workflow, build image, chạy test/train, tạo registry/runner, đổi GitHub settings hoặc thay đổi cụm.

## 6. Kế hoạch kiểm thử nghiệm thu sau khi tích hợp

Đây là kế hoạch trả lời yêu cầu ngày 05/11 về cách test khi hoàn thành, **chưa phải kết quả test**. Luồng dự kiến là từng bản được CI kiểm chứng → người vận hành xác nhận đúng release → pipeline triển khai và Verify. Cơ chế duyệt cụ thể còn cần kiểm visibility/plan; nếu không dùng được required reviewers thì thiết kế thao tác chọn release thủ công, không giả lập một approval gate không tồn tại.

### 6.1. Chuẩn bị

- Ghi baseline chỉ đọc: release/commit, image digest, runtime/DAG hash, deployment spec, trạng thái API/worker, active model và dataset ID, migration revision và trạng thái pause/dispatch.
- Chọn một thay đổi nhỏ có thể quan sát được, ví dụ thông tin phiên bản build không chứa bí mật. Nếu cần bổ sung endpoint/field phiên bản thì thực hiện như một thay đổi source riêng có review.
- Dùng checkout sạch và môi trường thử riêng cho lỗi có chủ đích. Với integration đầy đủ cần DB/MinIO thử riêng và Airflow/worker cấu hình cho môi trường thử; không chỉ đổi namespace nhưng vẫn trỏ vào production.
- Nghiệm thu gói phát hành giữ nguyên artifact đã được CI kiểm; không rebuild giữa lúc chờ duyệt và triển khai. Lưu kết quả theo release, không dùng log cũ làm bằng chứng lượt mới.

### 6.2. Ma trận kiểm thử

| Ca | Cách thực hiện khi tích hợp xong | Kết quả cần đạt |
| --- | --- | --- |
| CI phát hiện lỗi | Tạo PR thử có lỗi test/cú pháp chủ đích, không merge vào nhánh phát hành | CI đỏ; không publish release production hoặc gọi triển khai. Sửa lỗi thì CI xanh. |
| Checkout sạch | Chạy CI từ source Git trên runner sạch | Build/test các nhóm bắt buộc không cần runtime private hoặc thư mục results local; phần skip ghi rõ. |
| Chưa duyệt | Push bản hợp lệ, chờ CI/build đạt rồi kiểm version/spec trên cụm | Có artifact sẵn sàng; production vẫn là baseline trước khi xác nhận. |
| Từ chối phát hành | Reject job nếu có required reviewers, hoặc không khởi chạy bản phát hành ở cơ chế thủ công | Không cập nhật cụm. Với GitHub approval gate, job bị Reject phải kết thúc thất bại đúng thiết kế. |
| Đúng bản được duyệt | Bản A đạt kiểm tra; có thêm bản B trước khi duyệt A | Chỉ deploy A nếu A được chọn; đối chiếu commit/digest/hash thực chạy, không lấy latest hoặc rebuild thành B. Nếu baseline đã đổi thì preflight chặn và yêu cầu review lại. |
| Luồng thành công | Duyệt một bản thay đổi nhỏ đã đạt kiểm tra | Runner triển khai đúng phạm vi; API/worker đúng release; Verify và receipt đạt; trạng thái dispatch được khôi phục. |
| Phát hành lại cùng bản | Chọn lại release/digest đã đang chạy và baseline không đổi | Báo up-to-date hoặc không tạo thay đổi Pod template; không rollout/migration/training lặp chỉ vì chạy lại workflow. |
| Phạm vi thay đổi | Thử commit docs-only, frontend-only và backend liên quan riêng | Chạy đúng jobs theo đường dẫn/phụ thuộc; không cập nhật ứng dụng hoặc dịch vụ dữ liệu ngoài phạm vi release. |
| Training đang bận | Giả lập các trạng thái queued/running/finalizing/cancelling ở mock/môi trường thử | Chặn trước mutation; kiểm thêm trường hợp training tới đúng lúc deploy để chứng minh khóa dispatch có tác dụng. Không train production chỉ để tạo trạng thái bận. |
| Hai lượt phát hành | Khởi chạy hai release vào cùng môi trường thử | Chỉ một lượt thay đổi môi trường tại một thời điểm; không hủy ngang lượt đang triển khai; lượt sau kiểm lại baseline trước khi cập nhật. |
| Deploy lỗi và khôi phục | Dùng release có image/entrypoint hoặc readiness lỗi trong môi trường thử | Pipeline phát hiện lỗi, không báo thành công; ngừng bước tiếp theo và khôi phục bản tương thích khi điều kiện cho phép; xác nhận readiness/phiên bản sau phục hồi. |
| Giữ hành vi ứng dụng | So trước/sau bằng tài khoản và region thử đã chọn | Đăng nhập, danh sách region/model, GET forecast-dashboard, Quality/Importance và monitor hoạt động đúng; active model/dataset không tự đổi do deploy code; serving gate còn hiệu lực. |

Nguồn cho hành vi duyệt/từ chối: [GitHub reviewing deployments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments). Kubernetes báo trạng thái rollout; không coi rollout timeout là tự có rollback. Logic khôi phục và kiểm sau phục hồi phải do quy trình phát hành thực hiện: [Kubernetes Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/).

### 6.3. Thứ tự thực hiện và bằng chứng hoàn thành

1. Kiểm CI/package từ checkout sạch và các guard bằng mock/offline.
2. Kiểm end-to-end, các trường hợp lỗi/rollback trong môi trường thử tách biệt. Nếu chưa có môi trường này thì ghi rõ ca nào chưa được kiểm; không gọi kế hoạch là nghiệm thu đã đạt.
3. Người vận hành xác nhận một bản hợp lệ cho production; kiểm trước khi duyệt, sau triển khai và thử phát hành lại cùng bản khi phù hợp. Không cố tình đưa bản hỏng lên cụm đang phục vụ.
4. Lưu receipt gồm commit SHA, workflow run, artifact/image digest, kết quả từng ca, version/hash thực chạy và mức kiểm chứng. Thành công không chỉ là biểu tượng xanh của workflow: phải có bằng chứng bản được chọn chạy đúng và ứng dụng hoạt động.

Đợt nghiệm thu CI/CD không tự bao gồm training dataset khóa luận, Kaggle thật hoặc Activate. Các phép kiểm này cần phạm vi riêng nếu người dùng giao; bằng chứng fixture nhỏ chỉ xác nhận kỹ thuật pipeline.
