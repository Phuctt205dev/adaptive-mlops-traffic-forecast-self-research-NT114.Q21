# Cách đọc GitHub Actions và trình bày CI/CD

Bốn workflow đầu là các phần kiểm tra/phát hành của ứng dụng. Hai workflow
90/91 phục vụ vận hành. Số đầu tên giúp nhận diện trong sidebar; không quy định
tất cả phải chạy nối tiếp nhau.

| Nhãn trong Actions | Vai trò | Khi cần mở |
| --- | --- | --- |
| [01 Backend CI](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/workflows/backend-ci.yml) | Test, kiểm bốn image, đóng gói và thử cập nhật/phục hồi trên Kubernetes riêng | Giải thích bản code được kiểm trước production |
| [02 Backend CD](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/workflows/backend-deploy.yml) | Kiểm nguồn CI, chọn Manual/Auto, giao release qua runner Windows và Verify RKE2 | Giải thích triển khai production và kết quả phục hồi |
| [03 Frontend CI](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/workflows/frontend-ci.yml) | Cài dependency và kiểm build frontend | Giải thích kiểm tra frontend |
| [04 Frontend CD](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/workflows/deploy-frontend-pages.yml) | Kiểm backend tương thích, build và publish Pages khi cần | Giải thích giao diện được xuất bản |
| [90 CD Control](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/workflows/cd-control.yml) | Lưu lựa chọn Auto/Manual lâu dài | Khi thực sự cần đổi chế độ |
| [91 CD Retry](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/workflows/backend-deferred.yml) | Thử lại release Auto đã hoãn vì training bận, qua đúng policy | Khi giải thích xử lý công việc đang chạy |

`pages-build-deployment` là workflow động của GitHub Pages. Không xóa hoặc
disable nó chỉ để làm gọn danh sách. Không cần mở mục này trong phần demo chính.

## Trình bày backend

1. Mở **01 Backend CI**, chọn một lượt đã xanh. Tên lượt có số CI, loại sự kiện
   và nhánh, ví dụ `Backend CI #24 | push | master`. Commit được GitHub hiển thị
   kèm lượt chạy. Các lượt lịch sử trước đổi nhãn có thể vẫn mang tiêu đề cũ.
2. Ở **Summary**, trình bày sơ đồ job và bảng coverage. `scope` chọn bộ kiểm;
   `tests` kiểm hồi quy; bốn `image` kiểm tương thích; `package` ràng buộc artifact;
   `current-runtime` thử cập nhật thường; `legacy-migration` chỉ bắt buộc khi full;
   `acceptance` kiểm đủ bằng chứng. Các job image và hai suite có thể song song.
3. Mở **02 Backend CD**, chọn lượt có tên nguồn `CI #24` tương ứng. Với lượt
   gọi thủ công, tên ghi `CI run <ID>` theo input. Số thứ tự `#24` và run ID dạng
   số dài là hai định danh khác nhau; link **Source CI** trong Summary nối đúng lượt.
4. Ở Summary, chỉ ra mode đã chọn, commit, nguồn CI, deployment và recovery.
   `inspect` kiểm nguồn/chính sách; `deploy` gọi runner; `record-result` ghi kết quả
   và xử lý frontend phụ thuộc. Mở log từng job khi cần bằng chứng cụ thể.
5. Đối chiếu RKE2 bằng k9s hoặc receipt đã lưu. `up_to_date=true` là no-op đã
   Verify nên pod không đổi. Với cập nhật thực, nhóm mới được kiểm trước chuyển
   traffic, bản cũ được drain/flush rồi thu hồi. Rollback có kết quả riêng;
   rollback thành công không biến lượt phát hành lỗi thành thành công.

Nên mở sẵn một cặp CI/CD đã hoàn tất và cùng nguồn để trình bày. Chỉ mở log,
Summary và artifact cần thiết; không cần bấm **Run workflow** để chứng minh một
lượt lịch sử. Không đổi mode hoặc tạo training/triển khai mới chỉ để xem màn hình.

## Cặp minh họa profile kiểm gọn — 08/11/2026

- [Backend CI #25 | push | master](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/runs/42492000347): 546 test đạt, bốn image checks đạt, current **6/6**, migration **3/3** và gate acceptance thành công.
- [Backend CD #37 | CI #25 | release](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/runs/42494467501): CI tự kích hoạt Auto CD, cả ba job thành công; receipt no-op đã Verify, pod giữ nguyên.

Source `7438b07`. Current mất 15 phút 04, migration 25 phút 07; CI full mất
28 phút 56. Bộ thường gộp manifest-only vào cập nhật API và giữ một no-op cuối;
bộ legacy chỉ kiểm ba ca riêng về chuyển kiến trúc cũ. Lượt này sửa chính bộ
kiểm nên chạy cả hai. App/controller/workflow thông thường chỉ cần current;
legacy/bộ kiểm/lịch sử chưa rõ và kiểm định kỳ/thủ công vẫn yêu cầu full.
Chi tiết điều kiện và giới hạn số đo: [luồng CI/CD](CICD_UNIFIED_FLOW.md#8-phạm-vi-kiểm-ci-và-bằng-chứng).

## Mốc sắp xếp Actions trước khi rút gọn — 08/11/2026

- [Backend CI #24 | push | master](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/runs/42471585676): 544 test đạt, bốn image checks đạt, cả `current-runtime` và `legacy-migration` đạt 9/9, gate `acceptance` thành công.
- [Backend CD #36 | CI #24 | release](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/runs/42474256701): được CI #24 tự kích hoạt qua `workflow_run`; `inspect`, `deploy` và `record-result` thành công ở chế độ Auto.

Hai lượt kiểm source `d1cbfd9760fe15f9bef04ce25af5853b0575297c`. Đây là thay đổi
cách hiển thị Actions nên release tái dùng image đang phục vụ. Receipt xác nhận
`up_to_date=true`, `patched=[]` và có proof Verify. UID/cấu hình/số restart của
5 Deployment và 6 pod được theo dõi giữ nguyên. Dùng cặp này để giải thích
luồng CI → Auto CD và trường hợp không cần cập nhật pod; chọn một lượt có
thay đổi runtime thực nếu muốn minh họa rollout.

Frontend CI #3 cũng thành công. Frontend CD #39 chờ backend Verify nên bỏ qua
build/deploy; callback sau CD #36 xác nhận nội dung/cấu hình frontend đã được
publish, không tạo thêm lượt xuất bản giống hệt.

## Trình bày frontend và điều khiển

Mở **03 Frontend CI** để xem kiểm build và **04 Frontend CD** để xem bước chọn
source, build, deploy. Đây là hai workflow có trigger riêng; thứ tự nhãn không
có nghĩa workflow 03 luôn kích hoạt workflow 04. Frontend phụ thuộc backend
mới phải chờ backend Verify; frontend có nội dung/config đã publish có thể được
bỏ qua. Summary cho biết lý do, không coi mọi job skipped là lỗi.

**90 CD Control** có ô chọn chế độ yêu cầu. Tên lượt ghi `Request Auto` hoặc
`Request Manual`, chưa phải khẳng định mode đã lưu. Xem Summary sau khi job
thành công để biết mode/blocked thực tế. **91 CD Retry** là hỗ trợ tự động khi
training đang chạy; không phải một đường triển khai bỏ qua cổng an toàn.

## Hợp đồng phải giữ khi đổi cách hiển thị

Tên file workflow, job, artifact, concurrency và environment được controller
sử dụng. Đợt sắp xếp chỉ đổi nhãn/tên lượt và đồng bộ subscriber Backend CD
với nhãn CI mới. Giữ tên file giúp link hướng dẫn, workflow ID và API dispatch
tiếp tục trỏ đúng. Các job giữ nguyên tên để gate coverage và các kiểm ngoài
workflow tiếp tục hoạt động. Kiểm contract tại tests/test_workflow_navigation.py.
