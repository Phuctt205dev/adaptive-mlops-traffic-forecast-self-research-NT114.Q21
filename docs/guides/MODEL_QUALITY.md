# Model Quality: theo dõi 24 giờ gần nhất

Admin mở **Model Quality** ở sidebar. Trang tự dùng model active của khu
vực trên thanh đầu trang và 24 giờ đã hoàn tất gần nhất. Không cần chọn
nguồn, ngày bắt đầu, số giờ hoặc bấm Evaluate. Trang tự tải khi mở, cập
nhật mỗi phút khi hiển thị; nút Refresh dùng để thử lại ngay.

Ví dụ lúc **16:35 ngày 26/10**, múi giờ khu vực UTC+7: cửa sổ từ **16:00
ngày 25/10 đến trước 16:00 ngày 26/10**, gồm 24 điểm theo giờ. Chưa tính giờ
16:00–17:00 vì giờ đó chưa kết thúc. Dùng đồng hồ backend và múi giờ region,
không dùng đồng hồ trình duyệt hoặc ngày đầu/cuối dataset.

Đường xanh là lưu lượng quan sát được khai báo trong dataset; đường cam
là dự báo tính lại bằng model active với đầu vào lịch sử cùng giờ. Khoảng
cách giữa hai đường là chênh lệch; trỏ chuột hoặc dùng phím mũi tên xem
dự báo trừ thực tế và sai số tuyệt đối. Đây là replay của model hiện tại,
chưa phải bản lưu dự báo đã phát hành ở quá khứ hoặc giám sát camera trực tiếp.

Không có dữ liệu hiện tại thì giữ khoảng trống, không lùi sang ngày khác
hoặc dùng phần tương lai. Dòng thuộc tập train của model không được chấm điểm.

## Nguồn nhãn

- **Dataset reference (simulation / unverified)**: đối chiếu với các giá trị
  có sẵn, gồm cả dữ liệu được sinh/nội suy hoặc chưa xác minh. Reference
  MAE/RMSE chỉ mô tả mức bám dataset, không phải độ chính xác giao thông thật.
- **Historical observations (labelled rows only)**: chỉ đối chiếu dòng có
  `target_observed=true` (cũng nhận `1`). Nếu `target_is_imputed=true` hoặc
  `target_observed=false`, dòng đó là tổng hợp và không được tính trong chế
  độ này. Không có cờ thì nguồn là `unknown`, không tự coi là quan sát.
- Cờ là khai báo trong đúng dataset đang đánh giá, không phải xác nhận từ
  camera hoặc chứng minh tính xác thực bên ngoài. Không lấy file audit local
  mặc định để gán nhãn cho một dataset khác trên MinIO.
- Giao diện tự dùng chế độ quan sát. Không có cờ vẫn có thể hiện dự báo,
  nhưng không có đường thực tế và điểm sai số nếu nhãn chưa đủ điều kiện.
- Chế độ tham chiếu chỉ còn trong API phục vụ đối chiếu kỹ thuật, không
  xuất hiện như một lựa chọn trong giao diện admin.
- Dữ liệu thực tế bằng 0 hợp lệ. Nhãn thiếu, âm hoặc không hữu hạn không
  dùng tính điểm; giờ thiếu giữ khoảng trống, không ghép sang giờ kế tiếp.

Không thêm các cờ vào dataset đang dùng chỉ để làm xuất hiện điểm quan sát.
Nguồn gốc phải được xác minh từ dữ liệu gốc/quy trình tạo dataset trước.
Biểu đồ không chứng minh model cũ đã loại nhãn tổng hợp khi huấn luyện.

## API và phạm vi thực thi

`GET /api/v1/admin/regions/{region_id}/model-quality`

Mặc định `window=recent`, `mode=observed`, `hours=24`, không có `start_at`.
Cửa sổ recent từ chối mode tham chiếu, khoảng bắt đầu hoặc số giờ tùy chỉnh.
`model_version_id` tùy chọn để phát hiện model active đã đổi.

API còn giữ `window=replay` cho đối chiếu kỹ thuật theo khoảng tùy chọn:
`mode=reference|observed`, `hours=1..168`, `start_at` tùy chọn. `start_at`
không offset được hiểu là giờ địa phương của region; có offset thì chuyển
về region. Response có model/dataset ID, khoảng thời gian có offset, múi
giờ, thời điểm tạo, coverage, warnings và từng điểm reference/prediction/error.
`window_end_at` là cận kết thúc không bao gồm; `end_at` là giờ của điểm cuối.
Không có dữ liệu sau train thì available_start_at/available_end_at có thể null.

MAE/RMSE được tính trên cùng các cặp hợp lệ trong cửa sổ, không lấy CV/Test
MAE cũ để gắn vào biểu đồ. Không sửa DB, train, activate hoặc trigger DAG.
Những model đang hỗ trợ serving có nhánh replay tương ứng: legacy tree,
tree autoregressive, neural sequence. Nhánh autoregressive cập nhật lịch sử
sau khi dự báo mỗi giờ; nhánh neural giữ sequence kết thúc trước target.

Báo cáo ngắn được tính trực tiếp trên API khi mở tab hoặc tự cập nhật; cache
tối đa 4 báo cáo/60 giây mỗi process, gắn model/dataset/config/range/mode.
Đổi giờ thì không dùng cache cửa sổ trước.
Chỉ một report được tính mỗi process; khi có runtime RKE2 thì dùng chung
khóa inference và từ chối ngay nếu đang bận. Không tự fetch khi tab hoặc
cửa sổ trình duyệt bị ẩn. Không lưu prediction hay chạy training để tạo báo cáo.
Đánh giá toàn dataset/tác vụ dài cần chuyển sang worker và artifact bền vững
ở giai đoạn sau; chưa có luồng tạo artifact báo cáo trong bản này.

## Kiểm tra và triển khai

Chạy test offline: `python -B -m unittest tests.test_model_quality`.
Build frontend: `cd frontend` rồi `npm run build`.

Dùng thử trên môi trường đã cập nhật backend/frontend:

1. Đăng nhập admin, vào Model Quality ở sidebar: tự tải, không có form chọn.
2. Kiểm tra tên model/dataset, múi giờ, hai đường và bảng chi tiết từng giờ.
3. Dataset không có cờ: MAE/RMSE phải là dấu gạch ngang, không được hiện 0.
   Dự báo vẫn có thể hiển thị. Thiếu dữ liệu hiện tại không tự đổi ngày.
4. Kiểm tra cửa sổ có nhãn quan sát/tổng hợp đã biết, giờ thiếu và target=0.
5. Đổi region hoặc active model: báo cáo cũ phải được bỏ; thử lại dự báo cũ.
6. Chuyển sang Models hoặc tab khác: không tự gửi request đánh giá mới.

Source tự động 24 giờ này chưa được deploy. Backend replay thủ công trước
đó đã rollout thành công. Cần chuẩn bị gói nâng cấp từ đúng overlay live;
gói frontend/deployment cũ gắn hash nguồn cũ không dùng cho source mới.
Cần backend và frontend cùng phiên bản; backend
cũ chưa có endpoint thì giao diện thông báo chưa khả dụng, không dựng dữ
liệu minh họa thay cho kết quả. Không sửa source/hash runtime đã pin để ép
image cũ chạy source mới. Trước triển khai RKE2 cần review image/overlay
và cấu hình đang chạy theo quy trình của dự án; người dùng tự rollout.
