# Feature Importance và feature đang được model sử dụng

Chọn region rồi mở sidebar **Feature Importance**. Phiên bản được admin
Activate ở trang Models xác định **một lượt train** để xem cả hai khu vực.

1. **Average across five models:** trung bình báo cáo của Random Forest,
   XGBoost, LightGBM, LSTM và GRU trong đúng lượt đó. Gộp feature cùng ý
   nghĩa, chuẩn hóa mỗi report thành100%, rồi mỗi loại model đóng20%.
   Không lấy số phiên bản đã lưu làm trọng số hoặc gom các lượt train khác.
2. **Model details:** dropdown chọn một trong năm **loại model**, rồi xem
   báo cáo riêng của loại đó. Không chọn v1/v2 ở đây. Đổi dropdown chỉ đổi
   báo cáo, không Activate, train hoặc thay model phục vụ dự báo. Option
   **Currently serving** đánh dấu thuật toán đang phục vụ.

Chỉ vẽ trung bình khi có đủ5 report hợp lệ với đóng góp đo được. Nếu thiếu,
báo **N of5 model reports included**, giữ khu vực trung bình ở trạng thái
incomplete và vẫn cho xem riêng report hiện có. Report thiếu/lỗi/toàn0
không được coi là0% để vẽ trung bình giả. Một nhóm vắng trong report hợp lệ
góp0 vào tổng nhóm khi đã có đủ năm report.

Tỷ lệ thể hiện đóng góp tương đối vào dự đoán, không phải accuracy hoặc
chiều tăng/giảm. Model neural cộng độ lớn qua các giờ trong sequence rồi
lấy trung bình theo chuỗi; View details ghi số giờ. Không so điểm SHAP
tuyệt đối của tree với neural. Biểu đồ này không chọn feature cho retrain;
selector riêng từng model ở [SHAP_RETRAIN_SELECTION.md](SHAP_RETRAIN_SELECTION.md).

## Báo cáo sinh và lưu lúc nào

Train đầu vẫn dùng đầy đủ feature và tính SHAP. Retrain sử dụng các bộ
feature riêng theo policy hiện hành và tạo report cho những model được
chọn chạy. Local xử lý trên worker; cloud xử lý trên Kaggle.

Nhánh cây trước chỉ fit cuối/lưu report model thắng. Bản này bật
`explain_all_models` trong runner training: model thắng giữ cách lưu/đánh
giá Final Test cũ; các cây được chọn còn lại lần lượt fit trên Development,
tạo SHAP rồi giải phóng. Chỉ giữ JSON của chúng, không giữ thêm trọng số
hoặc đăng ký thêm ModelVersion. Model thắng được giải phóng trước vòng này
để không giữ hai model fit toàn bộ dữ liệu cùng lúc. Có thêm chi phí fit
cuối và SHAP của các cây còn lại; không lấy Final Test làm mẫu giải thích.

LSTM/GRU đã có final fit và SHAP; bản này giữ report, sequence/preprocessor
và input schema riêng của từng loại trong `shap_reports`. Finalize gộp các
report từ ba nhánh trước khi đăng ký **một candidate thắng**. Contract file/
SHA hiện có chuyển các JSON/preprocessor qua Kaggle→worker→MinIO; MLflow
lưu từng JSON riêng, registry giữ metadata. Không đổi schema DB/graph DAG.

Model cũ thường chỉ giữ report của model thắng; API tiếp tục cho xem report
đó, không mượn report từ phiên bản/dataset khác. **Không tự backfill hoặc
fit model cũ khi xem trang.** Để có đủ năm report mới: admin chủ động train
đủ năm model, chờ thành công, rồi Activate phiên bản mới khi phù hợp.
Nếu chỉ chọn một phần model để train hoặc tắt SHAP thì không có trung bình
đủ năm; dropdown sẽ giải thích những báo cáo còn thiếu.

## Drift: model active đang dùng feature nào

Bảng **Feature Scores** có cột **Active model**:

- **Used:** feature là đầu vào trực tiếp hoặc có cột mã hóa tương ứng.
- **Not used:** biết schema đầu vào và feature không nằm trong đó.
- **Unknown:** chưa có metadata/report đủ tin cậy, không tự đoán là bị loại.

Membership dựa trên `model_input_features` của model active; model cũ dùng
tên input trong report SHAP đã xác minh nếu có. `weather_type_*` được quy
về weather_type, is_holiday_binary về is_holiday. Điểm SHAP bằng0 vẫn có
thể là input **Used**. Bảng hiển thị tất cả score, bỏ giới hạn12 hàng cũ.

Dấu này luôn nói về **model đang active**, kể cả khi xem check lịch sử.
Nó không đổi giá trị score, số feature drift, threshold hoặc trigger
retrain. Đổi region/model active sẽ tải lại thông tin; thiếu metadata hiện
Unknown. Không chạy SHAP hoặc deserialize model để đánh dấu input.

## API và giới hạn

`GET /api/v1/admin/regions/{region_id}/feature-importance` vẫn require_admin.
Query model_version_id là ID active kỳ vọng để bắt đổi active→409, không
phải dropdown phiên bản. Giữ field report phục vụ ở top-level; thêm
`model_reports` (năm loại) và `model_average` (ready/incomplete, used_reports,
total_models5, features). Trung bình phiên bản lịch sử `history` đã được
thay thế. Không có active thì không tự chọn phiên bản khác.

Mỗi JSON tối đa512KiB, tối đa1000 input; SHA/context region/dataset/model
được kiểm trước khi hiển thị. Report bộ mới kiểm thêm training_run_id cả
cây và neural. Neural kiểm preprocessor SHA/path, sequence length, đơn vị
và phương pháp tổng hợp riêng từng entry. API chỉ đọc saved JSON trong
models root hoặc MinIO, không tải đường dẫn tùy ý; không trả artifact path
hoặc raw storage exception. Lỗi một model không che báo cáo các model khác.

Drift list trả thêm `active_model_feature_usage` với model ID/version,
input names và map raw feature→true/false/null. Không ghi annotation này
ngược vào kết quả detection để làm thay đổi lịch sử.

## Triển khai và dạo test

Gói mới mặc định `results/feature-importance/five-model-v1`, bundle
`6af7ad7253beadff55d3d8c925126778d02374038e5b4ad781065c935b001864`.
Baseline history-v2 đã phục hồi cache4Gi và Resume hoàn tất. Giữ các gói/
hash/journal cũ; không replay Apply hoặc recovery của đợt trước.

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py -B -X utf8 scripts/shap/rollout_five_model_reports.py Check
& $py -B -X utf8 scripts/shap/rollout_five_model_reports.py Apply
& $py -B -X utf8 scripts/shap/rollout_five_model_reports.py Verify
```

AI chuẩn bị/kiểm gói; user Apply. Helper giữ cache4Gi, image/DB/DAG, kiểm
idle/source/spec/UID và dung lượng restore trước rollout, ba entrypoint
đúng bundle. Pod Failed/Succeeded lịch sử bị loại khỏi đếm replica; các
pod Pending/Unknown/unready vẫn chặn. Verify xác minh schema model_reports/
model_average trên process API đang phục vụ. Push frontend lên GitHub
Pages không tự Apply backend. Không tự train, Activate hoặc đổi dữ liệu.

Sau Verify đạt, Ctrl+F5 và thử:

1. Kiểm tra dropdown có đủ năm loại; đổi loại không đổi model active.
2. Với phiên bản cũ, xem trạng thái thiếu report và report còn dùng được.
3. Với lượt mới đã train đủ năm và được Activate, xem coverage5/5, biểu
   đồ trung bình và từng report; đổi Activate thì cả bộ đổi đúng lượt.
4. Mở Drift, chọn check lịch sử nếu cửa sổ hiện tại chưa được kiểm; xem
   Used/Not used/Unknown và đủ các hàng feature. Đổi region/model để kiểm.

Kiểm tự động: test_all_model_shap (tiny tree thật/transport),
test_feature_importance/history (năm loại/context/mean/usage), neural
metadata/registry/tracking và hồi quy retrain. Browser checker
`scripts/shap/check_feature_importance_ui.py` dùng API synthetic,
desktop/mobile320–390px, không live writes. Số demo không phải benchmark.
