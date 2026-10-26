# Model Quality: biểu đồ đầu tiên cho admin

Trong **Models → Model Quality**, chọn nguồn đối chiếu, giờ bắt đầu theo múi
giờ region và cửa sổ 24 giờ hoặc 7 ngày, rồi bấm **Evaluate model**. Để trống
giờ bắt đầu sẽ lấy giờ đầu tiên sau `train_end_date` của model active.
Đây là phát lại từng giờ bằng đầu vào trong dataset, không phải dự báo tất
cả 7 ngày từ một thời điểm duy nhất hoặc giám sát camera trực tiếp.

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
- Dữ liệu cũ không có cờ vẫn xem được ở chế độ tham chiếu. Chế độ quan sát
  hiện dự báo nhưng không có điểm sai số nếu không có nhãn đủ điều kiện.
- Dữ liệu thực tế bằng 0 hợp lệ. Nhãn thiếu, âm hoặc không hữu hạn không
  dùng tính điểm; giờ thiếu giữ khoảng trống, không ghép sang giờ kế tiếp.

Không thêm các cờ vào dataset đang dùng chỉ để làm xuất hiện điểm quan sát.
Nguồn gốc phải được xác minh từ dữ liệu gốc/quy trình tạo dataset trước.
Biểu đồ không chứng minh model cũ đã loại nhãn tổng hợp khi huấn luyện.

## API và phạm vi thực thi

`GET /api/v1/admin/regions/{region_id}/model-quality`

Query: `mode=reference|observed`, `hours=1..168`, `start_at` tùy chọn,
`model_version_id` tùy chọn để phát hiện model active đã đổi. `start_at`
không offset được hiểu là giờ địa phương của region; có offset thì chuyển
về region. Response có model/dataset ID, khoảng thời gian có offset, múi
giờ, thời điểm tạo, coverage, warnings và từng điểm reference/prediction/error.

MAE/RMSE được tính trên cùng các cặp hợp lệ trong cửa sổ, không lấy CV/Test
MAE cũ để gắn vào biểu đồ. Không sửa DB, train, activate hoặc trigger DAG.
Những model đang hỗ trợ serving có nhánh replay tương ứng: legacy tree,
tree autoregressive, neural sequence. Nhánh autoregressive cập nhật lịch sử
sau khi dự báo mỗi giờ; nhánh neural giữ sequence kết thúc trước target.

Bản đầu tính một báo cáo ngắn trực tiếp trên API khi admin yêu cầu; cache
tối đa 4 báo cáo/60 giây mỗi process, gắn model/dataset/config/range/mode.
Chỉ một report được tính mỗi process; khi có runtime RKE2 thì dùng chung
khóa inference và từ chối ngay nếu đang bận. Không tự fetch khi mở tab ẩn.
Đánh giá toàn dataset/tác vụ dài cần chuyển sang worker và artifact bền vững
ở giai đoạn sau; chưa có luồng tạo artifact báo cáo trong bản này.

## Kiểm tra và triển khai

Chạy test offline: `python -B -m unittest tests.test_model_quality`.
Build frontend: `cd frontend` rồi `npm run build`.

Dùng thử trên môi trường đã cập nhật backend/frontend:

1. Đăng nhập admin, vào Models, đánh giá 24 giờ với Dataset reference.
2. Kiểm tra tên model/dataset, múi giờ, hai đường và bảng chi tiết từng giờ.
3. Đổi sang Historical observations với dataset không có cờ: MAE/RMSE phải
   là dấu gạch ngang, không được hiện 0. Dự báo vẫn có thể hiển thị.
4. Kiểm tra cửa sổ có nhãn quan sát/tổng hợp đã biết, giờ thiếu và target=0.
5. Đổi region hoặc active model: báo cáo cũ phải được bỏ; thử lại dự báo cũ.

Source này chưa được deploy. Cần backend và frontend cùng phiên bản; backend
cũ chưa có endpoint thì giao diện thông báo chưa khả dụng, không dựng dữ
liệu minh họa thay cho kết quả. Không sửa source/hash runtime đã pin để ép
image cũ chạy source mới. Trước triển khai RKE2 cần review image/overlay
và cấu hình đang chạy theo quy trình của dự án; người dùng tự rollout.
