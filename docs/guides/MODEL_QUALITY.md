# Model Quality: so dự báo với traffic_volume trong dataset

Admin mở **Model Quality** ở sidebar. Trang tự lấy model active của khu
vực đang chọn, đối chiếu 24 giờ đã hoàn tất gần nhất và cập nhật mỗi phút
khi tab hiển thị. Không cần chọn nguồn nhãn, ngày bắt đầu hoặc số giờ.

## Giao diện chính

- **Last 24 hours** và khoảng giờ đang xem.
- **Average difference**: sai số tuyệt đối trung bình (MAE), tính bằng xe/giờ.
- **Hours compared**: số giờ có cả giá trị dataset và dự báo, ví dụ 24/24.
- Đường xanh **Actual traffic**: cột `traffic_volume` trong dataset.
- Đường cam **Predicted traffic**: kết quả model tính cho đúng giờ đó.

Không hiện các đoạn hướng dẫn, ghi chú nhỏ hoặc metadata kỹ thuật trên
phần chính. Chữ nội dung từ 16px; biểu đồ tự co theo màn hình và vẫn giữ
đủ 24 giờ trên điện thoại. Thanh dưới biểu đồ hiển thị giờ, Actual, Predicted
và Difference (sai số tuyệt đối). Mặc định xem giờ cuối cùng; trỏ chuột
hoặc phím trái/phải đổi giờ. Chú giải màu và đơn vị vẫn được giữ để đọc
được biểu đồ. Không đổi công thức, API hay dữ liệu đối chiếu.

**View details** mặc định đóng: tên model, tên dataset, múi giờ, RMSE và
bảng theo giờ. Không đưa log, chính sách kỹ thuật hoặc cảnh báo nguồn nhãn
vào luồng sử dụng chính. Trường hợp thiếu dữ liệu/lỗi vẫn thông báo rõ.

## Cách đọc sai số

Ví dụ dataset ghi 5.000 xe/giờ, model dự báo 4.800: chênh lệch có dấu là
-200, sai số tuyệt đối là 200 xe/giờ. Average difference là trung bình
sai số tuyệt đối; không phải trung bình chênh lệch có dấu. RMSE trong
View details cho sai số lớn trọng số cao hơn. Cả hai càng thấp càng tốt,
dùng cùng các cặp hợp lệ. Không tự đổi thành phần trăm chính xác.

Mốc 16:35 ngày 26/10 theo giờ khu vực cho cửa sổ [16:00 ngày 25/10,
16:00 ngày 26/10), gồm 24 điểm từ 16:00 hôm trước đến 15:00 hôm nay.
Khoảng giờ trên UI ghi thời điểm điểm đầu và điểm cuối. Dùng đồng hồ
backend và múi giờ region; không đổi ngày nếu dataset thiếu. Giờ đang
diễn ra chưa được tính. Dữ liệu đã dùng để train không chấm lại.

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

Mặc định `window=recent`, `mode=reference`, `hours=24`, không có `start_at`.
Response có `reference_source=dataset_traffic_volume`, model/dataset ID,
timezone, khoảng giờ, coverage, warnings và từng cặp reference/prediction.
`window_end_at` là cận kết thúc không bao gồm; `end_at` là giờ điểm cuối.
Recent không nhận mốc bắt đầu, số giờ khác hoặc mode observed.
`model_version_id` giúp phát hiện model active đã đổi trong lúc yêu cầu.

Dự báo tính lại bằng model active trên input lịch sử; chưa phải nhật ký
dự báo đã phát hành hôm trước. Không nạp target hiện tại làm đầu vào dự
báo. Autoregressive cập nhật lịch sử sau prediction; neural giữ sequence
kết thúc trước target. Không đổi helper serving.

API giữ `window=replay`, `hours=1..168`, `start_at` và hai mode phục vụ
đối chiếu kỹ thuật. Observed chỉ dùng với replay riêng, không là mặc định
của giao diện 24 giờ. Metadata provenance không điều khiển phép so mặc định.

Cache tối đa 4 báo cáo/60 giây mỗi process, gắn model/dataset/config/mode
và cửa sổ giờ. Một report tính đồng thời, dùng chung khóa inference trên
RKE2. Tab khác/cửa sổ bị ẩn không tự tải. Không ghi DB/prediction, train,
activate, sửa dữ liệu hoặc trigger DAG để tạo báo cáo.

## Kiểm tra và triển khai

Chạy `python -B -m unittest tests.test_model_quality`; frontend build bằng
`npm run build`. Kiểm giao diện desktop/mobile, chữ dễ đọc, không có ghi
chú nhỏ, details đóng mặc định, số hiển thị đúng, hai đường, khoảng trống,
phím điều hướng và tự refresh chỉ khi tab đang hiển thị.

Lần chỉnh giao diện này chỉ thay frontend/tài liệu, không cần rollout
backend. Chuẩn bị metadata/hash và production dist mới, người dùng tự
publish bằng helper. Giữ nguyên review/artifact deployment cũ; không sửa
hash gói cũ để ép checker chạy qua.
