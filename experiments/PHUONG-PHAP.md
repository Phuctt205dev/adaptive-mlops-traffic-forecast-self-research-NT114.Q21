# Sổ tay phương pháp: chọn "cái tốt nhất" bằng số đo

> **Cập nhật:** 26/11/2026 · **Dùng cho:** mọi việc phải chọn một phương án (một chỉ số, một cách phát hiện drift, một cấu hình…) trong khóa luận này và các dự án sau.
> Mỗi giai đoạn thực nghiệm bổ sung bài học vào mục cuối file.

## Khung 7 bước

| Bước | Câu hỏi tự đặt ra | Sai lầm hay gặp |
| --- | --- | --- |
| 1. **Mục đích** | Chọn cái này **để làm gì**, cho ai dùng? | Đi tìm "chỉ số tốt nhất" chung chung, trong khi mỗi việc cần một thứ khác. |
| 2. **Ứng viên** | Có những lựa chọn nào? Liệt kê đủ, kể cả cái đang dùng. | Chỉ so 2 cái quen thuộc. |
| 3. **Tiêu chí** | Thế nào là "tốt"? Viết ra **trước** khi xem kết quả. | Xem kết quả rồi mới đặt tiêu chí cho hợp với cái mình thích. |
| 4. **Mốc so sánh** | Cách đơn giản nhất làm được bao nhiêu? | Khen một kết quả "cao" mà không biết cách đơn giản cũng cao như vậy. |
| 5. **Thử nghiệm** | Thử ở đâu để **lộ điểm yếu**? (dữ liệu khác quy mô, có giá trị 0, có thay đổi đột ngột…) | Chỉ thử trên dữ liệu "đẹp". |
| 6. **Quyết định** | Chọn gì, vì sao, đánh đổi gì? | Không ghi lý do, sau này không giải thích được. |
| 7. **Giới hạn** | Kết luận **không** đúng ở đâu? | Viết như thể đúng cho mọi trường hợp. |

## Thói quen khi làm thực nghiệm

- **Đoán trước rồi mới chạy.** Viết ra mình nghĩ kết quả sẽ thế nào; khi kết quả khác dự đoán là lúc học được nhiều nhất.
- **Bắt đầu từ ví dụ nhỏ tính tay** (vài dòng dữ liệu) để hiểu cách một con số được tạo ra, rồi mới chạy trên dữ liệu lớn.
- **Kiểm "thí nghiệm của mình có giống thật không"** trước khi kết luận (ví dụ: chạy lại một cấu hình đã có trên hệ thống và so con số).
- **Mỗi lần chỉ đổi một thứ** để biết thay đổi nào gây ra kết quả.
- **Ghi lại để chạy lại được:** dữ liệu nào, phiên bản thư viện nào, seed, lệnh chạy.
- **Ghi loại dữ liệu** (thật / giả lập / mô phỏng) ở mọi bảng.

## Bài học theo giai đoạn

### Giai đoạn 2 — dataset

- Kiểm nguồn gốc dữ liệu bằng cách **so khớp từng dòng** với nguồn công bố (dataset "Chicago" hóa ra là I‑94 Minneapolis, trùng 100%).
- Kiểm tính hợp lý của từng cột: một cột trùng hệt cột khác (`dew_point` = `visibility_in_miles`) là dấu hiệu số liệu không thật.
- Đừng tin nhãn múi giờ: kiểm bằng một biến độc lập (nhiệt độ so với ERA5, giờ cao điểm theo mùa).

### Giai đoạn 3 — chỉ số đánh giá

**3.1 — tính tay trên 5 giờ thật** (notebook `notebooks/E1-chi-so-danh-gia.ipynb`)
- Cùng một bảng sai số, "giờ nào tệ nhất" đổi theo cách đo (sai nhiều xe ≠ sai nhiều %). Phải nói rõ đang đo theo cách nào.
- Hai con số cùng gọi là "phần trăm" có thể khác nhau hơn 3 lần (MAPE 34,5% so với WAPE 10,8%).
- Muốn biết điểm yếu của một thước đo thì "làm khó" nó bằng tình huống có thật: đổi quy mô, giá trị 0, một lần sai rất nặng.
- Thư viện có thể trả về một con số khổng lồ thay vì báo lỗi (MAPE ≈ 10¹⁷% khi có giờ 0 xe), nên phải kiểm kết quả có hợp lý không chứ đừng chỉ xem nó có chạy hay không.
- Một con số "đẹp" (R² = 0,94) chỉ có nghĩa khi đặt cạnh mốc so sánh.

**3.2 — mốc so sánh ngây thơ**
- Dựng nhiều cách đoán đơn giản và đo trên **cùng tập giờ test** với model; ở I‑94 chỉ "trung bình cùng thứ–cùng giờ" đã đạt R² 0,949.
- Cách đơn giản mạnh nhất **tùy dữ liệu** (cao tốc: hồ sơ tuần; đường phố Glasgow: cùng giờ tuần trước) → mốc phải đo riêng từng nơi.
- Mốc so sánh dùng lịch sử dài cũng bị **drift** làm hỏng (phần train Glasgow có COVID nên hồ sơ tuần đoán thấp ≈ 29 xe/giờ).
- Chỉ số "so với mốc" (MASE) giải được vấn đề quy mô, nhưng mẫu số phụ thuộc giai đoạn train; khi so trên cùng một dataset, tỉ lệ MAE so với mốc trên cùng giờ test dễ đọc hơn.
- Kiểm cả **chiều** của một nhận xét (đoán cao hay thấp) và **năm nào** thật sự thấp nhất, đừng chỉ dựa vào hiểu biết chung (năm thấp nhất ở Glasgow là 2021, không phải 2020).

**3.3 — model thật, đối chiếu hệ thống**
- **Đối chiếu với hệ thống thật trước khi kết luận** (chạy lại đúng một lượt đã có và so từng chữ số). Nhờ vậy mới phát hiện đã gọi nhầm đường train cây.
- **Đọc mã để biết hệ thống thật sự làm gì**, đừng tin tên gọi: `random_forest_lag` của hệ thống không dùng lag.
- **Thử trên dữ liệu thật, nhiều nơi:** dữ liệu giả lập che mất điểm yếu của model (Glasgow kém mốc 2,2 lần) và lỗi xử lý khoảng thiếu dài (I‑94).
- **Sửa tạm thì ghi rõ** sửa gì, ở đâu, chỉ cho thí nghiệm (cờ `--patch-median-round`, ghi trong `summary.json`); không âm thầm đổi mã hệ thống.
- Việc chạy lâu nên **tách khỏi notebook** (script chạy nền, có theo dõi tiến độ), notebook chỉ đọc kết quả; dọn file tạm lớn ngay sau khi lưu xong thứ cần giữ.

**3.4 — chấm các thước đo**
- **Xem các thước đo có thật sự khác nhau không**: 7 chỉ số chỉ là 3 họ (|e|: MAE/WAPE/MASE; e²: RMSE/R²; phần trăm từng giờ: MAPE/sMAPE). Trên cùng dữ liệu, chỉ số trong một họ xếp hạng y hệt nhau.
- **Kết luận đúng theo mọi thước đo thì chắc** (cây có lag luôn hạng 1–3); chỗ các thước đo bất đồng là chỗ phải **chọn theo mục đích**.
- **Kiểm độ vững bằng lấy mẫu lại theo ngày** (bootstrap) trước khi nói "A tốt hơn B"; khi các model sát nhau, ngôi đầu đổi dễ.
- **Hỏi "chỉ số này để ý phần dữ liệu nào"** (giờ đêm hay giờ cao điểm) rồi đối chiếu với điều người dùng thật sự quan tâm.
- Một chỉ số cao ở mọi nơi (R² ≈ 0,9 trở lên ở cao tốc kể cả cách đoán ngây thơ) **không phân biệt được** tốt với xấu.

**3.5 — quyết định**
- **Không có "chỉ số tốt nhất" chung chung**, chỉ có chỉ số tốt nhất **cho một việc**: viết rõ việc, người dùng, yêu cầu quan trọng nhất trước khi chọn (chọn model ≠ hiển thị ≠ theo dõi drift).
- Mỗi lựa chọn đi kèm **bằng chứng đo được và đánh đổi**.
- **Minh họa quyết định trên dữ liệu thật** ("hiện nay / đề xuất"): ở Glasgow, con số tuyệt đối nhỏ nhất (28 xe/giờ) lại là model kém cách đoán đơn giản 40%.
- Tách **kết quả** (báo cáo E1) khỏi **đề xuất thay đổi hệ thống** (cần người có quyền quyết).

### Giai đoạn 4 — áp dụng vào hệ thống

- **Chạy lại bằng đúng hàm của hệ thống** (E2 gọi `_build_report` trên dự đoán của E1) để chắc phần đã sửa cho đúng con số của phân tích; nhờ vậy lộ ra một câu trong E1 thiếu tên model.

### Giai đoạn 5 — khi nào retrain

**5.1 — đo cách đang có**
- **Đo hệ thống có sẵn trước khi đề xuất cái mới**, bằng chính mã và cấu hình của nó (gọi `calculate_feature_drift`, chọn cửa sổ như `_check_region`).
- **Kiểm chứng âm:** dựng tình huống chắc chắn không có hiện tượng (kỳ hiện tại nằm trong kỳ tham chiếu). Bộ phát hiện drift hiện tại vẫn báo ≈ 98% → nó luôn bật, không mang thông tin.
- "Báo đúng lúc" chưa đủ: phải hỏi **nó có báo cả lúc không nên báo không**. Ở Glasgow, hệ thống có báo trong thời gian COVID nhưng đã báo từ hai tuần trước đó vì thời tiết đổi.
- Đọc hình trước khi viết kết luận: câu ban đầu "số cột vượt ngưỡng không tăng khi COVID" sai; hình cho thấy có tăng, nhưng cũng tăng y như vậy khi giao thông bình thường.

**5.2 — dựng đáp án**
- Đáp án định nghĩa bằng **hậu quả** (retrain có làm sai số 28 ngày tới giảm ≥ 15% không), không bằng chính tín hiệu đang chấm.
- **Kiểm tra đáp án trước khi dùng:** bản nháp (so từng tuần, model học 3.593 giờ) cho 47% tuần "nên retrain" ở nơi ổn định → vô lý → đổi sang quyết định mỗi thứ Hai, đo 28 ngày, cửa sổ 24 tháng như hệ thống (I‑94: 0–11%).

**5.3 — so nhiều phương án**
- Vẽ mọi phương án **trên cùng một dòng thời gian, cạnh đáp án**: nhìn là thấy cái nào báo bừa.
- Tín hiệu dựa trên sai số model chỉ tốt bằng **mức so sánh** của nó: "WAPE lúc train" đo trên 15% cuối cửa sổ dao động 5,8–17,6% tùy mùa, nên cùng ngưỡng 15% lúc quá chặt, lúc quá lỏng.
- Chưa kết luận khi ngưỡng mới là "ngưỡng tự nhiên"; dò ngưỡng và chấm bằng số ở bước sau.

**5.4 — chấm bằng số**
- Đặt **bắt đúng cạnh báo nhầm** (và độ trễ): cách hiện tại bắt đúng 100% nhưng báo nhầm 100%, điểm J = 0.
- **Dò ngưỡng trước khi so**; dò hết mà vẫn kém thì lỗi ở *thứ được đo*, không ở ngưỡng.
- **Giả lập có kiểm soát** (giảm 30% đột ngột/dần từ ngày biết trước) bổ sung cho dữ liệu thật: đo được độ trễ rõ ràng.
- Đọc lại câu chữ sau khi có số: ba câu viết trước khi chạy (khoảng báo nhầm, độ trễ drift dần, bảng tóm tắt) phải sửa theo số thật.

**5.5 — quyết định bằng mô phỏng cả vòng**
- **Chấm tín hiệu chưa đủ, phải mô phỏng cả vòng** báo → retrain → dùng model mới: ở 5.4 cách hiện tại tệ nhất, nhưng khi mô phỏng nó lại cho sai số thấp — vì luôn báo nên thành retrain hằng tuần, không vì phát hiện đúng.
- **Đặt lợi ích cạnh chi phí** (số lần retrain); chính sách "tốt hơn" có thể chỉ vì tốn hơn.
- Kết quả trái dự đoán là thông tin quý: giá trị của retrain ở hệ thống này đến chủ yếu từ **dữ liệu mới**, không từ **phát hiện drift**.
- Khi chạy việc lâu ở chế độ nền, **luôn bật theo dõi tiến độ** cho người dùng (lần dựng notebook 5.5 đầu tiên quên bật, người dùng không biết đã xong chưa).
