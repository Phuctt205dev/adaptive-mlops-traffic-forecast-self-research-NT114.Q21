# Đọc một lần phát hành trên GitHub Actions

Hướng dẫn source cải tạo10/11/2026; chưa thay xác nhận CI/CD production.
Mở **01 Application Delivery** cho luồng thường ngày. Các lượt cũ vẫn có tên
Backend CI/Backend CD và được giữ làm bằng chứng lịch sử.

## 1. Màn hình chính

| Phần trong một run | Đọc gì? |
| --- | --- |
| scope | So với bản production nào, cần backend/frontend/current/full hay không |
| tests | Regression của ứng dụng và công cụ phát hành |
| frontend-build | npm build duy nhất, dist và manifest đúng SHA/config/attempt |
| image (api/tree/neural/sync) | Build hay Reuse, kiểm image thực và digest |
| package | Các image được ràng buộc với đúng source/manifest |
| current-runtime / legacy-migration | Bộ sâu khi scope yêu cầu; skipped đúng scope là bình thường |
| acceptance | Mọi kiểm bắt buộc phải thành công; proof đúng run/attempt |
| backend-delivery / inspect, deploy, record-result | Mode/nguồn, runner Windows, component Deploy/Keep, kết quả deployment và recovery |
| frontend-publication / publish | Download và kiểm dist đã build, kiểm backend tương thích, publish Pages |
| summary | Kết quả chung của lần phát hành |

Frontend-only có thể bỏ backend delivery. Backend lỗi/deferred thì frontend
phụ thuộc phải chờ. PR/lịch tuần/Run workflow của01 không deploy production.
Chọn **Re-run all jobs** khi cần kiểm lại một run; không ghép proof các attempt.

## 2. Thao tác khi cập nhật ứng dụng

1. Push nhánh mặc định trong phạm vi được giao, mở run mới nhất của01.
2. Xem Scope để biết phần nào sẽ chạy; xem Build/Reuse của image.
3. Backend Manual: Review deployments → rke2-production → Approve and deploy.
   Auto đi qua policy đã lưu, không cần duyệt mỗi release.
4. Xem component Deploy/Keep trong Summary backend; thành phần giữ nguyên
   không được rollout chỉ vì commit mới. no-op vẫn có Verify.
5. Xem kết quả backend/recovery/Pages riêng. Deployment failed + recovery
   succeeded nghĩa là phát hành lỗi nhưng đã phục hồi, không phải rollout đạt.

Máy Windows, Tailscale và runner phải online. Không chạy Apply tay song song.
Không train/Activate thật để chứng minh màn hình CI/CD.

## 3. Các workflow hỗ trợ

| Workflow/file | Khi dùng |
| --- | --- |
| 02 Backend Delivery / backend-deploy.yml | Được01 gọi trong cùng run; Run workflow chỉ cho retry/release có nguồn CI đã kiểm, hoặc migration riêng đã review |
| 04 Publish Checked Frontend / deploy-frontend-pages.yml | Được01 gọi; retry thủ công nhập exact source_sha và artifact_run_id của build đã kiểm, không build lại |
| 90 CD Control / cd-control.yml | Lưu Manual/Auto, xem mode/blocked/owner; không phải chỉ báo live bằng ô checkbox |
| 91 CD Retry / backend-deferred.yml | Thử lại Auto deferred có budget, không vượt mode/chốt/ownership |
| 93 Frontend Check (manual) / frontend-ci.yml | Kiểm build thủ công khi cần chẩn đoán, không nằm trên đường push bình thường |

Retry Pages dùng cấu hình API/base-path đúng lúc build. Cấu hình đã đổi thì
cần build mới. Artifact hết hạn/không khớp SHA/attempt phải dừng.
`pages-build-deployment` do GitHub tạo vẫn có thể xuất hiện; không xóa nó để
làm gọn sidebar.

## 4. Tài liệu và giới hạn

[Luồng đầy đủ](CICD_UNIFIED_FLOW.md), [Manual/Auto và recovery](CD_MODES.md),
[thiết lập runner/RKE2](CICD_RKE2.md). Bằng chứng cặp CI #25/CD #37 ngày08/11
ở handoff lịch sử; chưa dùng cặp này để tuyên bố pipeline cải tạo đã chạy thật.
