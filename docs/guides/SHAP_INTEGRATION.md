# SHAP trong pipeline hiện có

SHAP chạy sau bước lưu model, không tạo pipeline training mới, không tự
chọn feature, train lại hoặc activate. Nhóm cây và tab admin đã triển khai
ở đợt trước. Source LSTM/GRU cho local/Kaggle đã nối lại ngày29/10; đã kiểm
mạng nhỏ trên worker CPU và Kaggle GPU, cần rollout riêng trước khi coi
hệ thống live hỗ trợ đủ năm model.
Xem FEATURE_IMPORTANCE.md và SHAP_NEURAL_ROLLOUT.md; giữ gói trees-first cũ
làm bằng chứng lịch sử, không chạy lại gói đã publish.

## Điểm nối

- `internal_training.execute_tree_training_branch` gọi `src/pipeline.py`:
  CV ba thuật toán, fit và lưu model thắng, rồi tính SHAP cho model đó.
  Không thêm lượt fit để giải thích các thuật toán bị loại.
- LSTM/GRU đi qua `src/region_training_variants.py` tới
  `src/time_series_cross_validation.py`. SHAP được tính trên model cuối
  sau khi lưu, trước khi giải phóng phiên Keras. Các fold CV không tính SHAP.
- Pipeline CV cây dạng chuỗi thời gian cũng gọi cùng module SHAP, dùng
  preprocessor đã fit để biến đổi mẫu. Không fit lại encoder/scaler.
- `src/shap_reporting.py`: `TreeExplainer` cho RF/XGBoost/LightGBM;
  `GradientExplainer` cho LSTM/GRU. Neural cần kiểm tương thích trên đúng
  TensorFlow/Keras; test không có TensorFlow sẽ báo skip, không tính là đạt.

## Báo cáo và tài nguyên

Mặc định lấy tối đa 32 mẫu trải theo phần training/development, background
neural mới dùng8 mẫu giải thích/background4/batch1/nsamples32 nếu không ghi
đè cấu hình; cây giữ32 mẫu/batch4. Cấu hình tường minh được kiểm và tôn trọng.
Không lấy Final Test
hoặc production để xếp hạng. Bộ đệm đầu vào/workload ước tính được kiểm
32 MiB; đây không phải giới hạn RAM toàn process. Ngân sách 60 giây được
kiểm giữa và sau các batch, không ngắt được một lời gọi SHAP đang chạy.

Chỉ lưu tổng hợp JSON: tên/thứ tự feature sau xử lý, model SHA256, context
region/dataset/run nếu có, nguồn và chỉ số mẫu, thời gian tính, phiên bản
SHAP, điểm ảnh hưởng và tỷ trọng. Không lưu ma trận SHAP cho toàn dataset.

Tree được kiểm tính cộng: giá trị nền + tổng SHAP phải khớp predict.
Neural dùng ước lượng gradient; nhân với scale của target để đưa đóng góp
về đơn vị traffic_volume. Tổng hợp độ lớn theo các giờ rồi lấy trung bình
các mẫu, đồng thời lưu ảnh hưởng theo lag. Hai kiểu tổng hợp không dùng
để so điểm tuyệt đối giữa tree và neural.

`shap_report.status` có `completed`, `disabled`, `unavailable` hoặc `failed`.
Lỗi SHAP không làm mất candidate đã train; cần kiểm status, không coi báo
cáo lỗi là feature importance. Lỗi/tắt SHAP không đổi cách chọn model.
Runner điều khiển local/Kaggle mới yêu cầu báo cáo neural `completed` khi
SHAP được bật; nếu tính lỗi, không xuất bundle thành công và bộ điều khiển
dừng/dọn lượt theo chính sách hiện hữu. `enabled:false` vẫn được giữ nguyên.

Tham chiếu file nằm ở `shap_report.source_report`, là trường bridge hiện
có hỗ trợ lồng nhau. Manifest/receipt/SHA256 truyền file qua MinIO và gắn
lại đường dẫn; registry lưu metadata trong configuration_json. Không sửa
runtime/hash/guard đã triển khai. Source local cần kiểm tương thích và
chuẩn bị deployment riêng trước khi dùng trên image live.

## Cấu hình từ yêu cầu training

Trường `shap` được truyền qua request tạo training run, worker và các lượt
retrain do drift. Ví dụ tắt cho một lượt:

```json
{"shap": {"enabled": false}}
```

Ví dụ giảm lượng tính toán:

```json
{"shap": {"max_samples": 8, "background_samples": 4, "batch_size": 2, "nsamples": 32}}
```

Mỗi số có trần được kiểm trước training; không có lựa chọn dùng toàn dataset.
`shap==0.50.0` đã thêm vào hai requirements (Python 3.11 hiện dùng).
Không cài thư viện vào pod hoặc tự rebuild image đang chạy.

## Giới hạn và bước tiếp

Báo cáo này giải thích model cuối đã học toàn Development. Nó được đánh dấu
`usable_for_feature_selection=false`: không dùng thứ hạng này để quay lại
chọn feature rồi công bố điểm CV trên chính các fold đã bị nhìn thấy.
Thực nghiệm rút gọn cần bộ chọn được fit trong phần train của từng fold,
chốt phương án bằng CV và chỉ dùng Final Test để đánh giá cuối.
Chưa có benchmark chứng minh tăng tốc hoặc tăng chất lượng.

Source legacy còn tạo dummy thời tiết theo từng batch (`drop_first=True`);
cần kiểm lại tính nhất quán train/dự báo trước thực nghiệm chọn feature.
Lượt này giải thích đầu vào model đã học, chưa đổi preprocessing/serving.

Kiểm thử nhỏ, không gọi DB/S3 thật hoặc train dataset dự án:

```powershell
.venv/Scripts/python.exe -B -X utf8 -m unittest tests.test_shap_reporting tests.test_shap_pipeline tests.test_region_pipeline tests.test_model_registry_service
.venv/Scripts/python.exe -B -X utf8 Deployment_test_on_rke2/kiem-thu/test-training-result-bridge.py
```

## Biểu đồ admin giai đoạn cây

Đã có source tab Feature Importance trên sidebar, thanh ngang xếp hạng
với tên dễ đọc và nhóm các cột mã hóa liên quan. Chi tiết kỹ thuật ở View
details. Tỷ trọng là phần của tổng độ lớn SHAP, không phải phần trăm dự
đoán đúng hay nguyên nhân gây ra giao thông. API/schema mới nhận báo cáo
GradientExplainer gắn đúng run/model/preprocessor và độ dài chuỗi; UI dùng
cùng thanh ngang, thông tin cửa sổ thời gian nằm trong View details.
Kiểm trên source không thay proof rollout/GPU. Heatmap chưa thuộc đợt này.

Nguồn phương pháp: [TreeExplainer](https://shap.readthedocs.io/en/latest/generated/shap.TreeExplainer.html),
[GradientExplainer](https://shap.readthedocs.io/en/latest/generated/shap.GradientExplainer.html),
[global bar plot](https://shap.readthedocs.io/en/latest/example_notebooks/api_examples/plots/bar.html).
