# Model Quality: so dự báo với traffic_volume trong dataset

Admin mở **Model Quality** ở sidebar. Trang tự lấy model active của khu
vực đang chọn và mặc định đối chiếu **7 ngày gần nhất**. Thanh Time range
cho chọn **Last 24 hours / Last 7 days / Last 14 days**. Không cần chọn
nguồn nhãn hay ngày bắt đầu. Tab tự tải khi mở và làm mới vào đầu mỗi giờ.

## Giao diện chính

- **Traffic comparison**, thanh chọn thời gian và khoảng giờ thực sự được đánh giá.
- **Average difference**: sai số tuyệt đối trung bình (MAE), tính bằng xe/giờ.
- **Hours compared**: số giờ đối chiếu trong khoảng đủ điều kiện; ví dụ
  72/72 nếu mới có 3 ngày sau train dù đang chọn 7 hoặc 14 ngày.
- Đường xanh **Actual traffic**: cột `traffic_volume` trong dataset.
- Đường cam **Predicted traffic**: kết quả model tính cho đúng giờ đó.

Không hiện các đoạn hướng dẫn, ghi chú nhỏ hoặc metadata kỹ thuật trên
phần chính. Chữ nội dung từ 16px; biểu đồ tự co theo màn hình và vẫn giữ
đủ khoảng thời gian trên điện thoại. Thanh dưới biểu đồ hiển thị giờ, Actual, Predicted
và Difference (sai số tuyệt đối). Mặc định xem giờ cuối cùng; trỏ chuột
hoặc phím trái/phải đổi giờ. Chú giải màu và đơn vị vẫn được giữ để đọc
được biểu đồ. Giữ cách tính MAE/RMSE và đối chiếu traffic_volume.

**View details** mặc định đóng: tên model, tên dataset, múi giờ, RMSE và
bảng theo giờ. Không đưa log, chính sách kỹ thuật hoặc cảnh báo nguồn nhãn
vào luồng sử dụng chính. Trường hợp thiếu dữ liệu/lỗi vẫn thông báo rõ.

## Cách đọc sai số

Ví dụ dataset ghi 5.000 xe/giờ, model dự báo 4.800: chênh lệch có dấu là
-200, sai số tuyệt đối là 200 xe/giờ. Average difference là trung bình
sai số tuyệt đối; không phải trung bình chênh lệch có dấu. RMSE trong
View details cho sai số lớn trọng số cao hơn. Cả hai càng thấp càng tốt,
dùng cùng các cặp hợp lệ. Không tự đổi thành phần trăm chính xác.

Khoảng đánh giá kết thúc ở đầu giờ hiện tại, chỉ gồm giờ đã hoàn tất.
Giờ bắt đầu là mốc muộn hơn giữa đầu khoảng đã chọn và train_end_date
(làm tròn lên giờ đầy đủ). train_end_date là ranh giới dữ liệu đã học,
không phải thời điểm bấm train hoặc thời điểm tác vụ train kết thúc.
Pipeline dùng dòng date_time < train_end_date làm tập train.

Ví dụ lúc 18:35 ngày 26/10, train_end_date là 18:00 ngày 23/10:

| Lựa chọn | Khoảng đánh giá | Số giờ tối đa |
| --- | --- | --- |
| 24 giờ | 18:00 ngày 25/10 đến trước 18:00 ngày 26/10 | 24 |
| 7 ngày | 18:00 ngày 23/10 đến trước 18:00 ngày 26/10 | 72 |
| 14 ngày | 18:00 ngày 23/10 đến trước 18:00 ngày 26/10 | 72 |

UI ghi thời điểm điểm đầu và điểm cuối (ví dụ kết thúc tại điểm 17:00).
Không lấy dòng thuộc tập train, không đổi ngày khi thiếu CSV. Chưa có
giờ đủ điều kiện sau train thì trả 0 điểm, metrics null, không lấy tương lai.

## Làm mới theo đầu giờ

Backend trả server_time và next_refresh_at. Frontend đặt hẹn đến đầu giờ
kế tiếp theo múi giờ khu vực: mở 18:35 thì tải tiếp lúc 19:00, 20:00.
Không còn setInterval một phút hoặc tính chu kỳ từ lúc mở trang. Response
cache vẫn có server_time mới để không đặt hẹn trễ; đổi giờ thì cache key đổi.
Đồng hồ máy admin sai không ảnh hưởng khoảng chờ tính theo server.

Tab/cửa sổ ẩn thì dừng hẹn và hủy request; mở lại tải mới rồi đặt lại hẹn.
Máy ngủ hoặc trình duyệt hạn chế nền không bảo đảm chạy đúng mili giây,
nhưng sẽ lấy dữ liệu mới khi trở lại. Request lỗi thử lại sau 30 giây;
Refresh thủ công không dịch mốc đầu giờ kế tiếp.

## Schema CSV hiện tại

`data/TrafficVolumeData.csv` có 15 cột:

| Vai trò | Cột |
| --- | --- |
| Thời điểm để ghép cặp | `date_time` |
| Ngày lễ | `is_holiday` |
| Môi trường | `air_pollution_index`, `humidity`, `wind_speed`, `wind_direction`, `visibility_in_miles`, `dew_point`, `temperature`, `rain_p_h`, `snow_p_h`, `clouds_all` |
| Loại/mô tả thời tiết | `weather_type`, `weather_description` |
| Giá trị cần dự đoán và dùng đối chiếu | `traffic_volume` |

Model dùng đầu vào qua xử lý serving hiện có. Biểu đồ chất lượng ghép
`date_time`, `traffic_volume` và prediction cùng model. Đóng góp của từng
feature thuộc biểu đồ SHAP ở bước sau.

Theo yêu cầu của người dùng, `traffic_volume` là chuẩn đối chiếu của chức
năng này. Không yêu cầu `target_observed` hay `target_is_imputed`, không
bỏ giá trị vì thiếu cờ nguồn. Không sửa dataset hoặc train lại để vẽ.
Nguồn là dataset của training run sinh model active; file local tham
khảo không tự thay dataset đang phục vụ.

Giờ/target thiếu giữ khoảng trống, không tự điền hoặc ghép sang giờ khác.
Target 0 hợp lệ; target âm/NaN/vô hạn không tính điểm. Không có cặp hợp
lệ thì metrics null và UI hiện dấu gạch ngang.

## API và cách tính

`GET /api/v1/admin/regions/{region_id}/model-quality`

Mặc định `window=recent`, `mode=reference`, `hours=168`, không có `start_at`.
Response có `reference_source=dataset_traffic_volume`, model/dataset ID,
timezone, khoảng giờ, coverage, warnings và từng cặp reference/prediction.
`window_end_at` là cận kết thúc không bao gồm; `end_at` là giờ điểm cuối.
Recent nhận hours trong 24/168/336, không nhận mốc bắt đầu hoặc mode observed.
`model_version_id` giúp phát hiện model active đã đổi trong lúc yêu cầu.

selected_hours là khoảng được chọn; coverage.requested_points là số giờ
thực sự sau khi cắt tại ranh giới train. end_at có thể null khi không còn
giờ hợp lệ. server_time mới ở mỗi response, generated_at là lúc tính
báo cáo; next_refresh_at là đầu giờ tiếp theo.

Dự báo tính lại bằng model active trên input lịch sử; chưa phải nhật ký
dự báo đã phát hành hôm trước. Không nạp target hiện tại làm đầu vào dự
báo. Autoregressive cập nhật lịch sử sau prediction; neural giữ sequence
kết thúc trước target. Không đổi helper serving.

API giữ `window=replay`, `hours=1..336`, `start_at` và hai mode phục vụ
đối chiếu kỹ thuật. Observed chỉ dùng với replay riêng, không là mặc định
của giao diện. Metadata provenance không điều khiển phép so mặc định.

Cache tối đa 4 báo cáo/60 giây mỗi process, gắn model/dataset/config/mode
và cửa sổ giờ. Một report tính đồng thời, dùng chung khóa inference trên
RKE2. Tab khác/cửa sổ bị ẩn không tự tải. Không ghi DB/prediction, train,
activate, sửa dữ liệu hoặc trigger DAG để tạo báo cáo.

## Kiểm tra và triển khai

Chạy `python -B -m unittest tests.test_model_quality`; frontend build bằng
`npm run build`. Kiểm giao diện desktop/mobile, chữ dễ đọc, không có ghi
chú nhỏ, details đóng mặc định, số hiển thị đúng, hai đường, khoảng trống,
phím điều hướng, cả ba khoảng, cắt tại train_end_date và trường hợp0 giờ.
Kiểm refresh đầu giờ với đồng hồ client bị lệch; tab ẩn không tự tải.

Lần cập nhật khoảng thời gian cần cả backend/frontend. Gói mới được chuẩn
bị từ đúng bản đối chiếu dataset đang chạy, kiểm entrypoint/dry-run rồi
người dùng tự apply; sau model_quality_ranges_ready mới publish frontend.
Giữ nguyên review/artifact cũ; không sửa hash gói cũ để ép checker chạy qua.
