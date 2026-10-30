# Feature Importance cho admin

Mở sidebar **Feature Importance**, chọn region ở thanh trên. Trang gồm
hai khu vực xếp dọc, cùng đọc báo cáo SHAP đã lưu:

1. **Across saved models:** đóng góp trung bình qua các phiên bản đã lưu
   trong region, gồm candidate/active/archived có báo cáo hợp lệ. Gộp cột
   cùng ý nghĩa, chuẩn hóa từng report thành100% rồi trung bình ngang nhau
   theo phiên bản. Không cộng điểm SHAP gốc của cây với neural.
2. **Active model:** báo cáo đúng model admin đã Activate tại trang Models.
   Không có bộ chọn v1/v2 riêng. Khu vực tổng quan vẫn xem được khi chưa
   có model active; lỗi báo cáo active không che báo cáo lịch sử hợp lệ.

Dòng **N of M saved models included** cho biết số báo cáo dùng/tổng model.
Mở **Reports included and excluded** để xem phiên bản nào thiếu/lỗi/chưa
hỗ trợ hoặc toàn điểm0. Những báo cáo này bị loại khỏi mẫu số trung bình,
không coi là đóng góp0. Nhóm vắng trong một report hợp lệ góp0 vào trung
bình; **Present in reports** phân biệt điều đó với report bị thiếu. Vắng
trong báo cáo không chứng minh feature không quan trọng với mọi model.

Đây là tổng quan lịch sử, không phải mỗi thuật toán một phiếu: thuật toán
có nhiều phiên bản sẽ có nhiều trọng số hơn. Có thể gồm nhiều dataset và
khoảng train trong cùng region; View details ghi số dataset và giải thích.

- Thanh dài hơn: model dựa nhiều hơn vào thông tin này trên các mẫu giải thích.
- Tỷ lệ là phần của tổng độ lớn SHAP, không phải phần trăm dự đoán đúng.
- Time of day gộp hour/hour_sin/hour_cos; Weather conditions gộp các cột
  mã hóa weather_type. View details giữ tên cột model và điểm gốc.
- Hỗ trợ cả cây và LSTM/GRU; không cần train hoặc backfill để mở trang.
- Với LSTM/GRU, điểm được cộng theo độ lớn qua các giờ quá khứ rồi lấy trung
  bình các chuỗi. View details ghi số giờ mỗi chuỗi; không so điểm tuyệt đối
  giữa tree và neural hoặc xem tỷ trọng là chiều tăng/giảm.
- Mở tab, đổi region/model hoặc bấm Refresh chỉ đọc JSON đã lưu. Không fit,
  nạp model lớn hoặc chạy SHAP trong request API; tab ẩn dừng request.
- Model cũ chưa có report hiển thị pending và bị loại khỏi trung bình,
  không bịa số liệu hoặc tự backfill.

API admin: `GET /api/v1/admin/regions/{region_id}/feature-importance`.
Client gửi model_version_id kỳ vọng để phát hiện model active đã đổi.
Response giữ các field báo cáo active, thêm `history` với features theo%,
total_models, used_reports, excluded_reports, dataset_count và danh sách
model kèm trạng thái report. Region không có active trả active ID null;
không gửi query model_version_id trong trường hợp đó. ID kỳ vọng cũ vẫn
trả409 nếu active đã đổi. Backend đọc toàn bộ phiên bản trong đúng region
bằng một streamed join; không cắt theo topN model. Không trả đường dẫn
artifact, SHA hoặc message exception storage ra bảng lịch sử.

Backend kiểm SHA256 report, context region/dataset và model SHA; đọc tối đa
512KiB từ cache models hoặc MinIO. Không tải file tùy ý ngoài models root.
Neural kiểm thêm training_run_id, preprocessor SHA đã ghi bởi producer,
preprocessor_file, sequence_length, đơn vị và cách tổng hợp. API không
deserialize model/preprocessor hoặc tính gradient khi xem báo cáo.

Mô hình huấn luyện mới sinh báo cáo tự động sau bước lưu model cuối. Luồng
cây legacy vẫn chỉ lưu model thắng CV; SHAP không làm phát sinh lượt fit
cho các thuật toán bị loại. Selector retrain riêng từng model đã thêm ở
[SHAP_RETRAIN_SELECTION.md](SHAP_RETRAIN_SELECTION.md); biểu đồ lịch sử
không được dùng làm selector hoặc thay cách lựa chọn model.

Kiểm thử: tests/test_feature_importance.py, tests/test_feature_importance_history.py,
tests/test_shap_reporting.py, tests/test_shap_pipeline.py và
`scripts/shap/check_feature_importance_ui.py` với API mô phỏng. Số trên ảnh
kiểm thử UI không phải kết quả model đang chạy.

Gói mở rộng lịch sử dựa trên neural-metrics-v1 đã Apply, không replay gói
trees-first/neural cũ. AI chuẩn bị source, ba startup proof và server
dry-run; người dùng triển khai backend bằng PowerShell tại project root:

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py -B -X utf8 scripts/shap/rollout_feature_importance.py Check
& $py -B -X utf8 scripts/shap/rollout_feature_importance.py Apply
& $py -B -X utf8 scripts/shap/rollout_feature_importance.py Verify
```

Review mặc định `results/feature-importance/history-v2`, source bundle
`04a19f466b70a420dfeeba0aa1d19ecd88956eee997caadde46661400920e345`.
Helper chỉ cập nhật source ConfigMap mount cho API/hai worker; giữ image,
schema DB, graph DAG và các cấu hình retrain. Check chặn khi training/
cleanup còn chạy hoặc source/spec/UID khác review. Apply có journal chống
lặp, pause DAG trong rollout và phục hồi trạng thái trước đó. Rollback
riêng của helper trả về neural-metrics-v1, vẫn hỗ trợ model đã rút gọn.
Không sửa hash/guard để ép qua kiểm tra hoặc dùng review từ clone khác.

Frontend được publish qua GitHub Pages khi push source frontend; việc
push không Apply backend. Nếu trang mới báo cần cập nhật backend ở khu
vực tổng quan, hoàn tất Apply/Verify trên trước khi nghiệm thu.

Thử sau triển khai: Ctrl+F5/đăng nhập admin, mở Feature Importance; xem hai
biểu đồ và số report được dùng, đổi region, thử vùng chưa active/nếu có,
Activate model khác tại Models rồi quay lại xem khu vực dưới đổi đúng.
Refresh chỉ đọc báo cáo; không cần bắt đầu training để thử giao diện.

Nếu history-v2 đã Apply dở và API bị Evicted vì `artifact-cache` vượt2Gi,
**không chạy lại Apply cũ**. Dùng review phục hồi riêng đã chuẩn bị:

```powershell
& $py -B -X utf8 scripts/shap/recover_feature_importance_cache.py Check
& $py -B -X utf8 scripts/shap/recover_feature_importance_cache.py Apply
& $py -B -X utf8 scripts/shap/recover_feature_importance_cache.py Verify
```

Recovery mặc định `results/feature-importance/cache-recovery-v1`: chỉ nâng
EmptyDir lên4Gi, requests đĩa API/init restore lên3Gi, limit đĩa API4Gi/
init5Gi. Sidecar limit1Gi giữ nguyên nên tổng limit đĩa container thường
là5Gi, đủ cho cache4Gi và overhead. Không tăng RAM, đổi image/source hoặc
xóa model/MinIO. Check chỉ đọc metadata kích thước và dung lượng node,
chặn nếu restore vượt80% cache mới hoặc node còn dưới6Gi. Guard giữ UID,
full spec và review/journal cũ; không sửa gói history-v2 để vượt guard.

Apply chỉ patch API đang dở, không rollout lại hai worker. Khi đủ hai API
ready và toàn bộ nguồn/health/schema history hợp lệ, helper phục hồi trạng
thái pause DAG theo journal ban đầu. Verify chỉ đọc; nếu Apply hồi phục
đã bắt đầu nhưng dừng giữa chừng, không replay Apply: chẩn đoán rồi dùng
`Resume` để kiểm và hoàn tất khi API đã ready. Dùng **Verify của recovery**
sau bước này; Verify gói history-v2 cũ sẽ từ chối spec đã tăng cache.

Nếu cache recovery đã tạo đủ hai API2/2 nhưng báo `The ready replica count
differs`, bộ Verify cũ đã đếm cả pod Failed/Evicted còn lưu trong Kubernetes.
Bản sửa trong `scripts/cloud_training/manage_rollout.py` bỏ qua phase
Failed/Succeeded và pod đang xóa; vẫn kiểm đủ replica, Running/ready, bundle,
flags, UID/spec và Airflow. Không cần xóa pod lịch sử để làm Verify đạt.
Sau khi có bản sửa và Verify đạt, hoàn tất recovery đang dở bằng:

```powershell
& $py -B -X utf8 scripts/shap/recover_feature_importance_cache.py Resume
& $py -B -X utf8 scripts/shap/recover_feature_importance_cache.py Verify
```

Resume không patch hoặc tạo pod lại: kiểm hệ thống, ghi hoàn tất recovery
và phục hồi pause state của DAG theo journal. Không chạy lại Apply/Check
trước patch vì spec đã ở trạng thái4Gi mới. Những dòng Error cũ trong K9s
là bản ghi các pod đã kết thúc; chúng không được tính là API đang phục vụ.
