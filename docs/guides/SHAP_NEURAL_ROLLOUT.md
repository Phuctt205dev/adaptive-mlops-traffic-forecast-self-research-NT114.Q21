# SHAP LSTM/GRU cho local và Kaggle

Source mới bổ sung GradientExplainer cho hai neural trong runner đang dùng,
báo cáo đi cùng model/preprocessor qua artifact/MinIO/registry. API chỉ đọc
báo cáo model active; biểu đồ giữ thanh ngang, số giờ quá khứ ở View details.
Không tự chọn feature, thay split/CV, activate hoặc đổi backend của run.

## Trạng thái và kiểm chứng

- Đã kiểm GradientExplainer thật cho hai mạng cực nhỏ chưa train trên
  TensorFlow2.21/Keras3.14/SHAP0.50 của worker; không dữ liệu nghiệp vụ.
- Kiểm thử ứng dụng/hồi quy và UI giả lập được ghi ở hand-off theo từng mốc.
- User đã cho phép gửi gói ML tối thiểu/dữ liệu tự sinh ra Kaggle riêng tư.
  LSTM/GRU nhỏ đã tạo báo cáo trên TensorFlow2.20/Keras3.13/SHAP0.50 GPU;
  helper Check/Apply vẫn yêu cầu proof đúng source và dọn tài nguyên đã xong.
- Source mới và gói review chưa đồng nghĩa đã rollout. Không dùng lại gói
  review-final, network/result/monitor-fix của đợt cloud trước để Apply bản này.

## Kiểm thử nhỏ

Tại thư mục main_project:

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py -B -X utf8 -m unittest tests.test_neural_shap_integration tests.test_neural_shap_rollout tests.test_feature_importance tests.test_shap_reporting tests.test_shap_pipeline -q
```

Test TensorFlow local có thể skip nếu chưa cài; skip không phải chứng minh
neural hoạt động. Dùng check_neural_compatibility.py để kiểm mạng rất nhỏ
trong tiến trình riêng trên worker rảnh, không model.fit hoặc đọc dữ liệu thật.

Phép thử Kaggle riêng tư không train model, dùng hai mạng nhỏ mới tạo và
dữ liệu tự sinh; kiểm gradient trên GPU, nhận báo cáo, đối chiếu model không
đổi rồi dọn chính tài nguyên thử. Chỉ chạy `run` sau khi được phép xuất mã ML.

```powershell
& $py -B -X utf8 scripts/shap/check_neural_kaggle.py prepare
# Lấy đúng đường dẫn "prepared" từ output, không chạy lại cùng job đã submit.
$probe = 'results/shap-neural/kaggle/<prepared-id>'
& $py -B -X utf8 scripts/shap/check_neural_kaggle.py run --directory $probe
```

Chỉ chấp nhận proof passed=true, remote_stopped=true,
private_staging_cleaned=true và đủ LSTM/GRU completed. Nếu mất liên lạc,
giữ ledger và đối chiếu; không submit lại để che trạng thái chưa rõ.

## Chuẩn bị, áp dụng và kiểm tra

User tự Apply/Rollback. Prepare, Validate và Check chỉ đọc/server dry-run;
kiểm entrypoint dùng thư mục tạm, không startup lifespan hay training thật.

```powershell
$kubectl = 'C:/Users/ADMIN/AppData/Local/Programs/kubectl/kubectl.exe'
$pkg = 'results/shap-neural/review-v2'
# Chỉ Prepare khi chưa có gói này; source thay đổi thì dùng tên gói mới.
& $py -B -X utf8 scripts/shap/rollout_neural_shap.py Prepare --kubectl $kubectl --directory $pkg
& $py -B -X utf8 scripts/shap/check_neural_runtime.py --kubectl $kubectl --directory $pkg --deployment traffic-training-tree
& $py -B -X utf8 scripts/shap/check_neural_runtime.py --kubectl $kubectl --directory $pkg --deployment traffic-training-worker
& $py -B -X utf8 scripts/shap/check_neural_runtime.py --kubectl $kubectl --directory $pkg --deployment traffic-api
& $py -B -X utf8 scripts/shap/rollout_neural_shap.py Validate --kubectl $kubectl --directory $pkg
```

Khi GPU proof đúng source đạt, chép nguyên proof vào gói, không sửa nội dung:

```powershell
Copy-Item -LiteralPath "$probe/proof.json" -Destination "$pkg/cloud-neural-proof.json"
& $py -B -X utf8 scripts/shap/rollout_neural_shap.py Check --kubectl $kubectl --directory $pkg
& $py -B -X utf8 scripts/shap/rollout_neural_shap.py Apply --kubectl $kubectl --directory $pkg
& $py -B -X utf8 scripts/shap/rollout_neural_shap.py Verify --kubectl $kubectl --directory $pkg
```

Helper giữ image, DB/migration, DAG và cờ cloud; đổi source ở hai worker/API,
thêm installer/dependency SHAP đã kiểm hash cho worker recurrent. Check yêu
cầu đủ proof và baseline không đổi. Apply giữ cửa sổ pause training khi idle,
đối chiếu UID/spec, kiểm runtime/capability rồi trả pause state ban đầu.
Lỗi giữa chừng giữ journal để đối chiếu, không replay Apply hoặc sửa hash.
Rollback chỉ khi không còn công việc và spec vẫn thuộc gói đã review:

```powershell
& $py -B -X utf8 scripts/shap/rollout_neural_shap.py Rollback --kubectl $kubectl --directory $pkg
```

Frontend có thay đổi ở View details. Đã có helper mới đóng gói build theo
đúng URL API/base path, khóa source/HEAD/remote và phạm vi file; preview
không stage/commit/push. Sau khi backend Verify đạt, user chạy:

```powershell
# Preview gói frontend đã được chuẩn bị cùng review-v2.
& $py -B -X utf8 scripts/shap/publish_neural_shap.py --directory $pkg
# Khi preview đạt, operator publish source đã review.
& $py -B -X utf8 scripts/shap/publish_neural_shap.py --directory $pkg --publish --kubectl $kubectl
```

Chờ neural_source_pushed, rồi đợi Pages workflow đúng commit thành công và
Ctrl+F5. Publish kiểm backend/private review và public API đúng bundle trước
ghi Git; giữ .gitignore, không force-add deployment/credential/proof. Nếu
push lỗi sau commit, giữ receipt và đối chiếu Git, không chạy lại helper mù.
Gói vẫn cần workspace deployment đầy đủ hiện có; việc chuyển toàn bộ runtime/
DAG ignored sang source clone được từ Git là việc bàn giao riêng đã ghi nhận
trong nhiệm vụ cloud, không được xử lý bằng force-add trong lượt publish này.

Nếu cần tạo frontend review mới sau khi đổi source, dùng một gói backend mới
và chạy Prepare build với URL/base path đã đối chiếu môi trường hiện hành:

```powershell
& $py -B -X utf8 scripts/shap/publish_neural_shap.py --directory $pkg --prepare --api-base-url 'https://<public-api>/api/v1' --base-path '/<repository>/'
```

## Dùng thử sau triển khai

- Khi được phép thực nghiệm, kiểm LSTM và GRU trên local và Kaggle, giữ
  lượng mẫu SHAP nhỏ. Theo dõi stage explaining và báo cáo completed.
- Candidate không tự active. Feature Importance vẫn đọc model active;
  không activate chỉ để kiểm báo cáo candidate. Có thể đối chiếu report/
  registry chỉ đọc trước khi user quyết định activate riêng.
- Model neural cũ chưa có báo cáo vẫn pending; Refresh không tự tính lại.
  Backfill model cũ là tác vụ riêng cần đúng model/preprocessor/dataset,
  không tự retrain hoặc chạy backfill chỉ vì đã rollout.
- Thiếu/lỗi báo cáo phải hiện thông báo; kiểm cây, Prediction/Model Quality
  vẫn hoạt động. Chưa dùng thứ hạng này để chọn feature hoặc kết luận tăng tốc.
