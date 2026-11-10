# Kiểm chứng cải tạo CI/CD

Gói source ngày 10/11/2026, chưa phải số đo GitHub/production sau phát hành.
Phép kiểm chính là trước/sau trên cùng ứng dụng; DORA là khung đánh giá delivery,
paper ICSE 2024 là nguồn phương pháp đo. Không có ngưỡng phút phổ quát để áp
cho dự án này. Xem [luồng CI/CD](CICD_UNIFIED_FLOW.md).

## Báo cáo tự động

Job `summary` trong **01 Application Delivery** thu metadata bằng GitHub GET,
đọc scope/acceptance/receipt của source và attempt được chọn. Nó không gọi
kubectl, build image, đổi CD Control hoặc chạy training. Lỗi collector được
báo rõ nhưng không đổi kết quả phát hành. Artifact `delivery-metrics-RUN-ATTEMPT`
giữ 30 ngày, gồm `delivery-metrics.json` và `report.md`; lưu bản cần nghiên cứu
trước khi hết hạn. Không đưa credential, runner name, log hoặc spec thô vào số đo.

| Số đo | Cách hiểu và giới hạn |
| --- | --- |
| `ci_wall` | Run created → acceptance kết thúc, gồm chờ. Chỉ đo attempt đầu; rerun không có mốc tạo attempt riêng đáng tin nên để unknown. |
| `ci_execution_span` | Job CI đầu bắt đầu → acceptance kết thúc; đo được cả rerun. |
| `initial_wait` | Run created → job CI đầu bắt đầu. Không phải phân tích đầy đủ queue hay thời gian reviewer duyệt. |
| `ci_runner` | Tổng thời lượng các job CI hoàn tất, có thể lớn hơn wall-clock do chạy song song. Chia 60 để ra runner-minutes; không phải số phút tính tiền. Không gồm summary/CD/Pages. |
| Thời gian từng job | Tests, từng role image, integration và package; thiếu timestamp giữ unknown. Image build/reuse được đọc từ step thực tế, không suy cache cold/warm. |
| Backend commit → Verify | Chỉ khi receipt xác nhận release đã thay đổi tài nguyên, hoàn tất và hết yêu cầu recovery. Đây là lead time của commit được chọn, không phải mọi commit trong batch. |
| Pages commit → publish | Step publish thành công trong cùng application run. Backend Verify và Pages là hai kết quả riêng. |
| Failure → verified rollback | `failed_at` → `recovered_at`, chỉ khi rollback đã hoàn tất và hết guard/recovery. Không thay xác nhận sự cố production đã hết; receipt cũ thiếu mốc giữ unknown. |
| Các pha backend | Chuyển pha trong `phase_history` tối đa 32 mốc. Phần chờ environment/runner trước engine và chờ training giữa nhiều run chưa được phân rã. |
| Thành phần | Build/reuse của image, kế hoạch deploy/keep từ receipt. Kế hoạch chưa phải bằng chứng thành phần đã được deploy; phải đọc outcome. |

Summary chạy khi workflow còn in-progress nên artifact tự động là **quan sát
đến trước khi summary kết thúc**, không gọi nó là tổng thời gian workflow.
Lượt bị cancel hoặc thiếu job bắt buộc thành công bị loại khỏi mẫu thời gian
CI đạt; outcome vẫn được đếm riêng. CI đạt nhưng CD lỗi vẫn giữ số đo CI.

`no_op`, `deferred`, `not_observed` không tính là deployment mới. Collector
giữ timestamp CI/Verify/Pages đã kiểm tra và run/attempt để đối soát cửa sổ đo.
Timestamp thiếu vẫn null; không lấy giờ ghi báo cáo thay giờ triển khai.
Collector
chưa tự tính deployment frequency, change fail rate hay deployment rework
rate: cần cửa sổ quan sát, danh sách deployment thật và đối soát incident/hotfix.
Màu đỏ CI hoặc preflight không tự chứng minh production incident.

## Thực nghiệm trước/sau

Giữ cùng business source/fixture, dependency lock, loại runner, resource và
điều kiện cache. Phiên bản workflow là biến được cải tạo. Nếu thay bộ kiểm,
báo rõ coverage đã đổi; không diễn giải phần giảm do bỏ job thành phần cứng
chạy nhanh hơn hoặc tương đương coverage.

| Kịch bản | Điều phải kiểm ngoài tốc độ |
| --- | --- |
| Frontend-only | Build một lần; Pages nhận đúng dist/config/source; backend không bị cập nhật khi runtime đầu vào giữ nguyên. |
| API-only | Image và deployment theo thay đổi thực của recipe/manifest; worker giữ nguyên nếu desired state không đổi. |
| Worker/DAG | API giữ nguyên nếu không liên quan; DAG đúng bản, không sinh training thật để thử CI/CD. |
| Runtime/manifest | Các case current phải chạy; phần legacy/contract không rõ vẫn full theo scope. |
| No-op | Không build/rollout thừa; Verify không bị gọi nhầm là deployment mới. |

Scope tính từ backend đã Verify, có thể tích lũy nhiều commit; không gán nhãn
frontend-only chỉ vì commit mới nhất sửa frontend. Recipe dùng chung vẫn có
thể làm nhiều role đổi source_id; bộ đo không hứa mọi sửa một file chỉ build
một image. BuildKit cache và reuse image là hai điều kiện khác nhau.

Pilot đề xuất **5–10 mẫu mỗi kịch bản và mỗi phương án**, median/min/max/N.
Đây là quy ước thực nghiệm của dự án, không phải ngưỡng DORA hay bảo đảm thống kê.
Tách push/PR/schedule, build/reuse/mixed, cold/warm, runner và workload. Không
dùng vài run khác nguồn, cache hoặc mức kiểm để kết luận phần trăm cải thiện.

Các repository variables tùy chọn `CI_BENCHMARK_EXPERIMENT`,
`CI_BENCHMARK_WORKLOAD`, `CI_BENCHMARK_CACHE_STATE` ghi điều kiện thực nghiệm;
mặc định unknown, không cản pipeline thường. Chỉ ghi `warm`/`cold` khi có bằng
chứng kiểm soát cache. Runner profile tự động `ubuntu-24.04` là nhãn workflow,
không chứng minh các máy có CPU/tải bằng nhau. Trong phép thử cần mô tả resource
cụ thể bằng nhãn `--runner-profile` và hồ sơ thiết lập đi kèm.

Có thể dùng CLI đọc một run đã hoàn tất, không rerun hoặc deploy nó:

```text
python -B scripts/cicd/benchmark.py collect --run-id RUN_ID --attempt 1 --inputs results/cicd/benchmark-input --output results/cicd/benchmark-sample --experiment pilot1 --workload fixture-v1 --cache-state warm --runner-profile runner-spec-v1 --scenario API-only
```

`RUN_ID` là placeholder cần thay bằng run thật. Collector dùng repository và
token đọc đã cấu hình trong môi trường (`GITHUB_REPOSITORY`, `GH_TOKEN`), không
ghi chúng vào hồ sơ. Chuẩn bị từ artifact đúng run/attempt, không sửa bằng chứng:

- `scope/ci-scope.json` và, nếu có, `acceptance/ci-acceptance.json`.
- `receipt/delivery-SHA-RUN_ID-ATTEMPT.json` của application run hiện tại.

Đọc được schema scope/acceptance của pipeline cũ để đo CI trước cải tạo, giữ
đúng coverage current luôn chạy của bản đó. CD cũ là run khác nên **không ghép
receipt cũ vào CI run để tạo lead time**; cần hồ sơ liên kết CI/CD riêng nếu muốn
nghiên cứu end-to-end lịch sử. Frontend CI độc lập của bản cũ chưa có adapter
trong collector này; không dùng CI backend cũ thay số đo frontend end-to-end.
CLI này cũng chưa ghép các run retry backend/
Pages độc lập vào run gốc. Những số đo đó giữ unknown; không bỏ mất chúng khi
tổng hợp ổn định phát hành theo hồ sơ vận hành.

So sánh các JSON thật đã lưu (truyền tên từng file; không phụ thuộc shell glob):

```text
python -B scripts/cicd/benchmark.py compare --baseline baseline-1.json baseline-2.json --candidate candidate-1.json candidate-2.json --output results/cicd/benchmark-comparison.json
```

Ví dụ trên chỉ minh họa cú pháp hai file, **chưa đủ 5 mẫu**. Công cụ chia nhóm
theo experiment/workload/scenario/cache/runner/image mode/event; không gộp
khác điều kiện, không nhận run/attempt lặp. Chỉ xuất phần trăm giảm cho metric
có ít nhất 5 mẫu mỗi bên, nhãn điều kiện đầy đủ và event push. Với số mẫu ít
hơn/unknown, vẫn báo median/range/N nhưng kết luận `insufficient_samples_or_conditions`.
`coverage_changed` báo sự thay đổi các bộ kiểm. Phần trăm pilot là mô tả mẫu,
chưa phải kiểm định nhân quả hay độ ổn định dài hạn.

## Điều kiện thử ổn định trước khi tiếp tục phát triển

1. Local regression, actionlint và frontend build đạt; ghi rõ phần không kiểm
   được trên Windows. Unit fixture kiểm công thức không phải mẫu benchmark thật.
2. Chạy GitHub CI đầy đủ cho gói thay đổi controller/workflow; các case current
   và migration phải có proof đúng SHA/attempt. Chạy nhiều push nhanh kiểm việc
   hủy CI cũ và chặn source cũ trước claim; không hủy transaction production.
   Trước merge có thể Run workflow trên nhánh thử nghiệm: frontend luôn được
   build ở lượt full; image/package vẫn chạy, backend/Pages deploy bị chặn
   bởi event khác push. Image dùng cho kind vẫn phải được publish sau checks.
3. Kiểm scenario matrix trên môi trường tách biệt: đúng artifact/component,
   giữ queued training/model active; case rollout/flush failure phải phục hồi
   theo bằng chứng. Không dùng production/CD45 làm phép gây lỗi.
4. Trước nghiệm thu backend thật: đọc mode/ownership/failure latch hiện tại và
   inventory cache. Preflight mới chỉ phát hiện lỗi vượt budget sớm; chưa giải
   quyết nguyên nhân CD45, không tự tăng ngưỡng hoặc xóa cache.
5. Thu đủ pilot tương đương rồi quyết định tối ưu tiếp từ job chậm thực tế.
   Không giảm kiểm đúng artifact, queued training hoặc rollback để đạt số phút.

## Nguồn phương pháp

- [DORA metrics](https://dora.dev/guides/dora-metrics/): khung delivery outcomes,
  đo cùng application và diễn giải theo bối cảnh.
- [Bouzenia & Pradel, ICSE 2024](https://software-lab.org/publications/icse2024_workflows.pdf),
  DOI 10.1145/3597503.3623303: nghiên cứu resource usage/optimization của GitHub
  Actions; nguồn phương pháp, không lấy thời gian/giá lịch sử làm mục tiêu hiện tại.
- [GitHub workflow run attempts](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run-attempt),
  [jobs theo attempt](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt):
  danh tính và timestamp của lần chạy được đo.
