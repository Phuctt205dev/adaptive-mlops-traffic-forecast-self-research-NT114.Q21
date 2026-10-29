# Feature Importance cho admin

Mở sidebar **Feature Importance**, chọn region ở thanh trên. Biểu đồ đọc
báo cáo SHAP của đúng model active và dataset đã tạo model đó.

- Thanh dài hơn: model dựa nhiều hơn vào thông tin này trên các mẫu giải thích.
- Tỷ lệ là phần của tổng độ lớn SHAP, không phải phần trăm dự đoán đúng.
- Time of day gộp hour/hour_sin/hour_cos; Weather conditions gộp các cột
  mã hóa weather_type. View details giữ tên cột model và điểm gốc.
- Source mới hỗ trợ cả cây và LSTM/GRU; cần triển khai bản neural theo
  SHAP_NEURAL_ROLLOUT.md. Bản live trees-first cũ vẫn thông báo chưa hỗ trợ.
- Với LSTM/GRU, điểm được cộng theo độ lớn qua các giờ quá khứ rồi lấy trung
  bình các chuỗi. View details ghi số giờ mỗi chuỗi; không so điểm tuyệt đối
  giữa tree và neural hoặc xem tỷ trọng là chiều tăng/giảm.
- Mở tab, đổi region/model hoặc bấm Refresh chỉ đọc JSON đã lưu. Không fit,
  nạp model lớn hoặc chạy SHAP trong request API; tab ẩn dừng request.
- Model cũ chưa có report hiển thị pending, không bịa số liệu. Sau rollout,
  helper `--explain-active` tạo báo cáo từ model đã có mà không train lại.

API admin: `GET /api/v1/admin/regions/{region_id}/feature-importance`.
Client gửi model_version_id kỳ vọng để phát hiện model active đã đổi.
Backend kiểm SHA256 report, context region/dataset và model SHA; đọc tối đa
512KiB từ cache models hoặc MinIO. Không tải file tùy ý ngoài models root.
Neural kiểm thêm training_run_id, preprocessor SHA đã ghi bởi producer,
preprocessor_file, sequence_length, đơn vị và cách tổng hợp. API không
deserialize model/preprocessor hoặc tính gradient khi xem báo cáo.

Mô hình huấn luyện mới sinh báo cáo tự động sau bước lưu model cuối. Luồng
cây legacy vẫn chỉ lưu model thắng CV; SHAP không làm phát sinh lượt fit
cho các thuật toán bị loại. Chọn feature/train rút gọn là bước thực nghiệm sau.

Kiểm thử: tests/test_feature_importance.py, tests/test_shap_reporting.py,
tests/test_shap_pipeline.py và kiểm browser với API mô phỏng. Số trên ảnh
kiểm thử UI không phải kết quả model đang chạy.

Triển khai theo Deployment_test_on_rke2/tai-lieu/README-shap-tree-admin.md.
Người dùng chạy apply/publish; không chạy lại package cũ sau commit hoặc
khi baseline thay đổi, không sửa hash/guard để ép qua kiểm tra.
