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
metric thực đang có; số liệu độ trễ/lỗi request có từ khi API tự ghi số đo
(mục Website service).

## Website service (sẵn sàng và số đo request)

Hàng thứ tư của System Overview trả lời "người dùng có được phục vụ tốt không",
theo thực nghiệm [E4](../../experiments/E4-san-sang-chiu-loi-chiu-tai.md):

| Ô | Ý nghĩa | Màu |
| --- | --- | --- |
| Prediction API · available (7 days) | % thời gian 7 ngày có ít nhất 1 pod API sẵn sàng (readiness 5 s). Là sẵn sàng, chưa phải mọi request thành công. | Xanh ≥ 99,5%, cam ≥ 99%, đỏ thấp hơn |
| Response time · 95% of requests (1 h) | 95% request của người dùng trong 1 giờ được trả lời trong thời gian này (bỏ health check). | Cam từ 1 s, đỏ từ 2,5 s |
| Failed requests (1 h) | % request trả lỗi máy chủ (5xx), gồm cả phản hồi "busy" khi pod đã đầy hàng chờ dự báo. 4xx (nhập sai, chưa đăng nhập) không tính. | Cam từ 1%, đỏ từ 5% |
| Website requests · last 24 h | Request từ website tới API trong 24 giờ. Mọi request này đi qua ngrok miễn phí (20.000/tháng ≈ 667/ngày). | Cam khi vượt phần của một ngày |

Không có request trong giờ thì hai ô giữa hiện **No requests** (không phải 0). Mục
thu gọn **Request details** có biểu đồ độ trễ 50%/95% và request/giây theo nguồn.

Số đo request do backend tự ghi (`backend/app/core/metrics.py`): số request,
thời gian trả lời (histogram) và request đang xử lý, gắn nhãn theo mẫu route
(không theo đường dẫn thật), mã trạng thái, phương thức và nguồn:
`web` (website qua ngrok), `proxy` (qua Nginx từ mạng riêng, ví dụ kiểm tải),
`cluster` (Airflow, worker), `probe` (health check của kubelet). Không lưu IP.
Pod API phục vụ số đo ở **cổng riêng 9464**; đường public (ngrok → Nginx →
NodePort 30080) chỉ tới cổng 8000 nên `/metrics` không lộ ra ngoài. Đặt
`TRAFFIC_METRICS_PORT=0` để tắt. Prometheus lấy số đo qua PodMonitor
[`api-podmonitor.yaml`](../../deployment/observability/api-podmonitor.yaml)
(mỗi 15 s, chọn cả hai slot blue/green theo `traffic.mlops/role=api`). PodMonitor
không đi qua CI/CD; **người dùng áp dụng một lần sau khi bản có cổng 9464 đã
chạy** (áp dụng trước sẽ làm cảnh báo TargetDown bật):

```powershell
kubectl apply -f deployment/observability/api-podmonitor.yaml
kubectl -n traffic-mlops get podmonitor traffic-api-requests
```

Prometheus chỉ giữ 7 ngày; số liệu cần cho báo cáo phải xuất ra file trước khi
hết hạn (`experiments/scripts/e4_prometheus_availability.py`).

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
