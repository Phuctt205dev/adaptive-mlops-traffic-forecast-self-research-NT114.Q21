# 90 CD Control: Manual / Auto

Code vẫn push vào `master` như hiện tại; CI kiểm/build như trước. Chế độ chỉ
chọn có cần duyệt deployment trước khi giao việc cho runner.

## Bật/tắt bằng GitHub Actions

1. Mở [Actions → 90 CD Control](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/workflows/cd-control.yml) → **Run workflow**, chọn nhánh `master`.
2. Ô **Desired mode**: đánh dấu chọn **Auto**, bỏ đánh dấu chọn **Manual**.
3. Bấm **Run workflow**, chờ bước lưu thành công.
4. Xem **Summary → mode** và **blocked** để biết kết quả đã lưu.

Đây là ô chọn *chế độ muốn lưu*, không phải chỉ báo trực tiếp trạng thái hiện
tại; GitHub mặc định bỏ chọn. Job hiển thị trạng thái cũ rồi kết quả lưu mới.
Chỉ coi việc đổi chế độ đã xác nhận khi job thành công. Auto được lưu qua các
lần push; không cần bật lại. Đổi mode không tạo release, build image hoặc thay pod.

Nhánh riêng `cd-settings` giữ `cd-state.json`, không chen commit vào master.
Không thực thi mã từ nhánh cấu hình. Chỉ job điều khiển/kết quả nhận Contents
write cho token job; không thêm PAT cá nhân. Thiếu cấu hình mặc định Manual;
state chưa khởi tạo hoặc không hợp lệ phải chặn triển khai.

| Chế độ | Sau CI thành công |
| --- | --- |
| Manual | `rke2-production` yêu cầu Review deployments → Approve and deploy. |
| Auto | `rke2-production-auto` đi thẳng tới runner, vẫn kiểm nguồn/commit và cụm. |

Cùng namespace, chỉ nhánh master, cùng startup/guard/rollout/Verify/rollback.
Windows/Tailscale/runner vẫn cần online. Training đang hoạt động thì release
được ghi DEFERRED trước mutation; backend-deferred kiểm lại đúng CI/mode/epoch
trước thử lại. Không hủy công việc đang chạy hoặc xóa hàng chờ để triển khai.

Công tắc áp dụng cho backend. Luồng thống nhất bổ sung gate cho frontend:
Pages chỉ publish khi nội dung backend tương ứng đã Apply+Verify hoặc không
đổi so với bản đã Verify. Backend đạt gọi lại Pages cho frontend mới nhất
tương thích. Bản frontend đã publish được ghi riêng, không thay mode/chốt CD.
Luồng này đã được nghiệm thu; xem [trạng thái và hợp đồng hiện hành](CICD_UNIFIED_FLOW.md)
và [cách đọc Actions](ACTIONS_DEMO.md). Migration runtime ngày 07/11 đã hoàn tất.

## Job cũ khi đổi chế độ

- Release chốt mode/epoch trong inspect, rồi nhận quyền triển khai bằng CAS.
  Nếu mode đổi trước lúc nhận quyền, release được skip trước thao tác trên cụm.
- Release đã nhận quyền tiếp tục hoàn tất/phục hồi. Đổi công tắc không cắt
  ngang transaction hoặc tự duyệt một yêu cầu đang chờ.
- Job chờ duyệt vẫn giữ gate; bạn có thể hủy job cũ **chưa triển khai** trên
  GitHub. Nếu duyệt sau khi mode đổi, bước claim sẽ skip. Job chờ cũ có thể giữ
  hàng đợi, nên xem Actions khi chuyển mode.
- Không ngắt rollout đang chạy hoặc triển khai CI cũ hơn bản đã phát hành.

`RKE2_CD_ENABLED=false` vẫn ngăn release mới ở inspect, không dừng transaction
đang chạy. Chọn Auto không tự bật công tắc này.

## Lỗi và bằng chứng

Workflow vẫn **FAILED** dù rollback thành công. Summary tách deployment và
recovery; artifact `delivery-receipt-<commit>-<attempt>` giữ receipt/chẩn đoán
đã lọc trong 30 ngày. Gửi link run thay vì chụp kịp pod đỏ.

Trước rollback, controller thu trạng thái pod/container, reason/exit code,
error type và tên file/dòng traceback với budget 12 giây. Không upload raw
spec/env, exception message, Secret hoặc log thô. Nếu mất kết nối/budget hết,
báo phần không thu được và vẫn ưu tiên rollback.

Sau lỗi, Auto bị chặn. Chuyển Manual, kiểm nguyên nhân/phục hồi rồi phát hành
một bản Manual đạt Apply+Verify; kết quả xác nhận mới gỡ chốt. Sau đó bật Auto
lại. Đổi công tắc không tự xóa chốt lỗi.

Nếu runner mất kết nối/receipt chưa chứng minh nhả guard, quyền sở hữu release
được giữ để chặn triển khai chồng. Không sửa JSON để xóa quyền này.

## Xác nhận phục hồi quyền sở hữu bị kẹt

Chủ vận hành đọc receipt gốc/hiện trạng và hoàn tất phục hồi bằng đúng artifact,
journal theo [guide CI/CD](CICD_RKE2.md). Sau đó `recover_cd.py` kiểm chỉ đọc:
đúng namespace UID, đủ năm Deployment đúng UID/spec trước (hoặc sau nếu receipt
đã completed), đủ replica Ready, guard kết thúc, DAG phù hợp và không training.
Chỉ sau bằng chứng mới xóa quyền sở hữu; Auto vẫn chặn tới Manual release đạt.

```powershell
# GH_TOKEN được cấp quyền, chỉ giữ trong bộ nhớ; không ghi log.
python -B scripts/cicd/recover_cd.py `
  --receipt <original-delivery-journal.json> `
  --kubectl <kubectl.exe> --kubeconfig <dedicated-kubeconfig> `
  --context traffic-delivery --namespace-uid <reviewed-namespace-uid>
```

Thiếu receipt, cấu hình đổi xen vào hoặc guard chưa rõ thì phải dừng và tái lập
phương án phục hồi có kiểm chứng. Không sửa receipt hoặc replay Apply để thử.

Trường hợp controller cũ đã claim nhưng `deliver.ps1` lỗi xác minh gói ở dòng 28,
chưa gọi engine và chưa tạo receipt, có nhánh kiểm chứng riêng:

```powershell
python -B scripts/cicd/recover_preapply.py `
  --kubectl <kubectl.exe> --kubeconfig <dedicated-kubeconfig> `
  --context traffic-delivery --namespace-uid <reviewed-namespace-uid> `
  --proof-output <ignored-local-proof.json>
```

Mặc định chỉ đọc. Helper lấy source/log của đúng run/attempt từ GitHub, chỉ
chấp nhận phiên bản controller lịch sử đã review và lỗi trước engine. Helper
tải ZIP receipt cuối thành công, kiểm digest/CRC, đối chiếu đủ 14 Deployment/
Service đúng UID/spec, replica Ready, DAG/training idle và không guard còn chạy.
Receipt vẫn giữ nguyên commit gốc; proof phục hồi là tài liệu riêng.

Nếu kiểm chứng đạt, chạy lại cùng lệnh với `--release` để kiểm tra mới và nhả
ownership bằng compare-and-swap, đồng thời kiểm baseline chưa đổi. Auto vẫn bị
chặn. Chọn Manual qua **90 CD Control**, phát hành CI thành công mới nhất và
người dùng duyệt `rke2-production`; Apply+Verify đạt mới xóa chốt lỗi. Không
rerun CD cũ để thử. Controller không khớp, có receipt hoặc bằng chứng thiếu
thì nhánh này từ chối và giữ ownership.

## Chuẩn bị một lần

`setup.ps1 -Action ConfigureCDModes` chuẩn bị environment Auto chỉ master và
khởi tạo Manual nếu chưa có. Giữ reviewer Manual, mode đã lưu và CD enable;
không đăng ký lại runner hoặc apply manifest nền.

Không dùng CI trước bản nâng cấp mode-control với workflow mới vì thiếu helper.
Khôi phục ứng dụng bằng rollback có kiểm chứng, không replay một CI cũ ngẫu nhiên.
Controller thống nhất còn yêu cầu runtime contract trong source CI. Chuyển
legacy sang runtime mới là thao tác `migrate-role-runtime-v1`, chỉ qua Manual;
release thường ngày chọn `release`, dùng cùng engine và không đoán layout cũ.

Có thể bật thông báo workflow lỗi trong [cài đặt GitHub Actions cá nhân](https://docs.github.com/en/subscriptions-and-notifications/how-tos/managing-github-actions-notifications).
Gói này không tự bật email/Slack thay user. Lỗi pod sau khi workflow hoàn tất
vẫn cần giám sát riêng; không hứa rollback mọi lỗi muộn hoặc zero downtime.

## Nghiệm thu ngày 06/11/2026

- Backend CI đạt **469/469 test**, build image, kiểm startup Linux và Windows Plan.
- [Manual triển khai thật](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/runs/42169054932)
  đạt Apply, Verify và ghi kết quả; guard đã nhả.
- Bật Auto một lần rồi chạy liên tiếp ba lượt cùng release:
  [lượt 1](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/runs/42170721748),
  [lượt 2](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/runs/42171022387),
  [lượt 3](https://github.com/Phuctt205dev/adaptive-mlops-traffic-forecast-self-research-NT114.Q21/actions/runs/42171414721).
  Cả ba không có approval, đều Verify/ghi kết quả thành công và không patch
  Deployment hoặc tạo lại pod. Đây là kiểm tra no-op, không phải ba rollout mới.
- Chuyển Auto → Manual → Auto thành công, không đổi UID/spec năm Deployment
  và UID sáu pod. Trạng thái cuối Auto, không bị chặn, không transaction dở;
  public readiness đạt và release đúng bản đã nghiệm thu.

Các ca lỗi/CrashLoop, rollback và chốt chặn Auto được kiểm bằng fixture/mock;
không cố gây lỗi pod production hoặc chạy training để nghiệm thu công tắc.

Runner dùng bundle CA công khai riêng cho Node/Python và vẫn kiểm TLS/hostname.
Nếu Python báo lỗi chuỗi chứng thư, kiểm CA/chuỗi issuer mà Windows xác thực
và làm mới bundle riêng trước khi chạy tiếp; không tắt TLS hoặc sửa state để
bỏ qua kiểm tra. Bundle không chứa khóa riêng và không đưa vào Git.
