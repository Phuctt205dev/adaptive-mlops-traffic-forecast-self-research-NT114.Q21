# Dashboard dành cho admin

Hai dashboard chính giữ nguyên UID và đường dẫn:

- **System Overview** (`traffic-system-overview`): trạng thái nền tảng, node,
  tám thành phần ứng dụng, tài nguyên cao nhất và cảnh báo cần xem.
- **Nodes** (`traffic-rke2-nodes`): ba cột cho ba node, thanh Processing/Memory/
  Storage và dung lượng còn dùng được. Lịch sử mặc định thu gọn.

Nhãn tiếng Anh, chữ ngắn, trạng thái bằng cả chữ và màu. Định nghĩa nằm trong
tooltip, không đặt đoạn hướng dẫn dài trên màn hình. Chi tiết cảnh báo/lịch
sử mở khi cần. Dữ liệu thiếu hoặc quá180s hiện Unknown, không mặc định là0%.

Thiết kế tham khảo [Grafana dashboard best practices](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/)
về cấu trúc và xem từ tổng quan tới chi tiết, cùng [NN/g — Aesthetic and Minimalist Design](https://www.nngroup.com/articles/aesthetic-minimalist-design/)
về giảm thông tin cạnh tranh với nội dung cần chú ý. Dùng thanh tài nguyên
cho so sánh các node, giữ màu trạng thái nhất quán, tránh biểu đồ trang trí.

## Ý nghĩa trạng thái

Ready là Kubernetes readiness, không phải xác nhận mọi HTTP request, kết quả
train hoặc độ chính xác model. Tám thành phần gồm Prediction API, hai training
controller, PostgreSQL, MinIO, MLflow và hai thành phần Airflow.

API/tree/neural gom tên deployment cũ và các slot blue/green vào một thành
phần. Slot có Desired0 không được coi là ứng dụng sẵn sàng. Một slot đang
phục vụ đủ replica có thể giữ trạng thái Ready trong khi slot staging khởi
động; dashboard này không nghiệm thu quá trình CD. Tên/policy ở
`scripts/observability/dashboards.py` cần cập nhật khi kiến trúc thay đổi.

Processing là CPU non-idle trung bình5phút. Memory là1-MemAvailable/MemTotal;
Storage là1-avail/size của filesystem gốc `/`. Ngưỡng dự án: chú ý80%, đỏ95%
CPU hoặc90% RAM/đĩa. Đây là mức xem xét ban đầu, không phải SLO hay kết luận
dịch vụ hỏng. Dung lượng Available không phải ngân sách đã dành cho pod mới.

Monitoring hiển thị warning/critical đang firing từ Prometheus; bỏ heartbeat
và informational. Dịch vụ gửi thông báo chưa kết nối vẫn phải hiện cảnh báo,
không xóa alert rule hoặc tô xanh để dashboard trông đẹp hơn. Chỉ dùng các
metric thực đang có; chưa tạo số liệu độ trễ/lỗi nghiệp vụ khi chưa instrument.

## Nguồn và cập nhật

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py -B -X utf8 scripts/observability/dashboards.py
& $py -B -X utf8 -m unittest tests.test_grafana_admin tests.test_grafana_catalog -q
```

JSON import ở `deployment/observability/dashboards/`. Chỉ hai dashboard chính
được cập nhật bằng API Grafana, với backup và optimistic version check:

```powershell
$review = 'results/grafana-review-new-session'
& $py -B -X utf8 scripts/observability/grafana_admin.py plan --directory $review
& $py -B -X utf8 scripts/observability/grafana_admin.py apply --directory $review
& $py -B -X utf8 scripts/observability/grafana_admin.py verify --directory $review
# Khi cần phục hồi và dashboard chưa bị sửa ngoài luồng:
& $py -B -X utf8 scripts/observability/grafana_admin.py rollback --directory $review
```

Các lệnh nhận `--kubectl`, `--kubeconfig`, `--context` khi cần. Namespace cố
định monitoring; credential dùng trong sidecar, không ghi ra file hoặc Git.
Không gọi lại Apply nếu lỗi kết nối chưa rõ kết quả; đọc trạng thái/receipt
và đối chiếu backup trước. Mất version/source hash thì dừng để review lại.

CI/CD ứng dụng hiện hành không cài dashboard Grafana. Push lưu source và
kiểm hồi quy; Verify ở trên và trình duyệt Grafana mới kiểm bản dashboard thật.
Không sửa workflow/runner hoặc triển khai lại backend để đổi bố cục monitoring.

## Dọn danh sách dashboard

Giữ hai bảng admin ở gốc, gom21 bảng chuyên sâu vào thư mục **Technical**.
Gỡ ba bảng không phù hợp cụm Linux duy nhất: **Node Exporter / AIX**,
**Node Exporter / MacOS**, **Kubernetes / Compute Resources / Multi-Cluster**.
Prometheus / Overview vẫn giữ trong Technical để chẩn đoán giám sát.

Các bảng mặc định được provision từ ConfigMap; xóa qua UI không xử lý nguồn.
Gói catalog chỉ đổi nhãn/annotation của nguồn dashboard và bật phân thư mục
trong provider. Giữ nguyên JSON nguồn ba bảng đã gỡ để có thể phục hồi.
Không xóa ConfigMap, pod, datasource, metric, alert rule hoặc dữ liệu Prometheus.
Tham khảo [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/#dashboards)
và [k8s-sidecar folder annotations](https://github.com/kiwigrid/k8s-sidecar#usage).

```powershell
& $py -B -X utf8 -m scripts.observability.catalog plan --directory $review
& $py -B -X utf8 -m scripts.observability.catalog dry-run --directory $review
# Apply thay đổi Kubernetes chỉ theo quyền được giao hoặc do user thực hiện:
& $py -B -X utf8 -m scripts.observability.catalog apply --directory $review
# Nếu provider mount bằng subPath, nhận cấu hình mới bằng restart Grafana
# một lần, chỉ khi user đã giao rõ bước restart:
kubectl -n monitoring rollout restart deployment/traffic-monitoring-grafana
kubectl -n monitoring rollout status deployment/traffic-monitoring-grafana --timeout=180s
& $py -B -X utf8 -m scripts.observability.catalog verify --directory $review
# Rollback chỉ khôi phục các field đã review; không force ghi đè drift:
& $py -B -X utf8 -m scripts.observability.catalog rollback --directory $review
```

Provider trong Helm hiện tại mount `provider.yaml` bằng **subPath**: sửa
ConfigMap rồi reload nóng chưa đủ; lần đầu bật phân thư mục cần restart riêng
Grafana theo quyền user giao. Deployment hiện dùng Recreate và dữ liệu nằm
trên PVC; kiểm lại hai điều này trước restart. Chỉ Grafana gián đoạn khi nhận
cấu hình mới, không restart Prometheus hoặc workload ứng dụng.

Verify chờ sidecar cập nhật và reload riêng provider Grafana; script catalog
không tự restart pod. Nếu chưa đạt, giữ backup/receipt và xem xét trước khi thử
lại. Sau Helm upgrade cần kiểm lại catalog/provider, không giả định chart mới
sẽ giữ các lựa chọn dashboard này. Backup và plan có resourceVersion/UID nằm
trong results ignored, không đưa credential hoặc manifest private vào Git.
