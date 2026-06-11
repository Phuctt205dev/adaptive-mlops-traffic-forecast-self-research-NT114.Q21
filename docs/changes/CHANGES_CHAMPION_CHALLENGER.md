# Thay đổi cơ chế retrain và phát hiện drift

## Mục tiêu

Không tự động thay model production sau mỗi lần retrain. Model mới phải được
đánh giá công bằng với model đang chạy trên cùng một khoảng dữ liệu chưa dùng
để huấn luyện model mới.

## Vòng đời mới

```text
Phát hiện drift
    |
    v
Huấn luyện candidate bằng rolling train window
    |
    v
Lưu candidate_model.pkl, chưa thay best_model.pkl
    |
    v
Cửa sổ thời gian kế tiếp
    |
    v
Champion và Candidate dự đoán cùng một dữ liệu
    |
    +-- Candidate giảm MAE >= 5%: promote
    |
    `-- Không đạt: reject và giữ Champion
```

Candidate được tạo sau khi kiểm tra cửa sổ hiện tại. Vì vậy, cửa sổ kế tiếp
được dùng làm promotion window và chưa xuất hiện trong dữ liệu train Candidate.

## Thay đổi theo file

### `src/pipeline.py`

- Xóa khối code cũ đã comment.
- Dùng `random_state=42` để kết quả có thể tái hiện.
- Chọn thuật toán theo MAE, cùng metric với monitoring.
- Dùng `TimeSeriesSplit` ba fold thay cho một validation slice duy nhất.
- Huấn luyện lại thuật toán thắng trên toàn bộ development set.
- Hỗ trợ lưu model theo vai trò `champion` hoặc `candidate`.
- Trả metadata model cho worker.

### `src/drift.py`

- Thêm `get_historical_mae_baseline()`.
- Baseline là median MAE của các production window gần nhất.
- Chỉ dùng lịch sử của đúng model version đang chạy.
- Loại các cửa sổ đã drift để MAE bất thường không làm baseline tăng dần.
- Chưa đủ số cửa sổ thì tắt ngưỡng tỷ lệ và vẫn giữ ngưỡng MAE cứng.

### `retrain_job.py`

- Thêm vòng đời Champion-Challenger.
- Candidate được lưu riêng, không ghi đè Champion.
- So sánh hai model trên cùng promotion window.
- Chỉ promote khi đạt `MIN_PROMOTION_IMPROVEMENT`.
- Thay file Champion theo cách nguyên tử để API không đọc file dang dở.
- Ghi mọi quyết định vào `monitoring/promotion_history.csv`.
- Ghi trạng thái promotion và MAE hai model vào `drift_state.json`.
- Baseline không còn lấy từ `test_MAE` của lần retrain.

### `docker-compose.yml`

Các biến mới:

```yaml
BASELINE_HISTORY_WINDOWS: "6"
MIN_BASELINE_WINDOWS: "3"
DEGRADATION_RATIO: "1.2"
MIN_PROMOTION_IMPROVEMENT: "0.05"
```

- Baseline dùng median sáu cửa sổ gần nhất.
- Cần ít nhất ba cửa sổ production.
- Drift tương đối khi MAE cao hơn baseline 20%.
- Candidate phải giảm MAE ít nhất 5% mới được promote.

Đã bỏ thuộc tính `version`, vì Docker Compose mới không còn cần nó.

### `tests/test_model_lifecycle.py`

Kiểm tra:

- Baseline chỉ lấy đúng model version.
- Baseline dùng median của lịch sử gần nhất.
- Chưa đủ lịch sử thì không tạo baseline.
- Candidate được promote/reject đúng theo mức cải thiện.

## File theo dõi trên EC2

### Lịch sử drift

```text
monitoring/drift_history.csv
```

Các cột quan trọng:

- `model_version`
- `current_mae`
- `baseline_mae`
- `ratio_threshold`
- `drift`

### Lịch sử promotion

```text
monitoring/promotion_history.csv
```

Các cột quan trọng:

- `champion_version`
- `candidate_version`
- `champion_mae`
- `candidate_mae`
- `improvement_ratio`
- `decision`

### Candidate đang chờ đánh giá

```text
models/candidate_model.pkl
models/candidate_model_info.json
```

Hai file này tồn tại nghĩa là Candidate sẽ được đánh giá ở cửa sổ kế tiếp.

## Cập nhật triển khai

Sau khi đưa code mới lên EC2:

```bash
docker compose down
docker compose up --build -d
docker compose logs -f traffic-drift-worker
```

State cũ vẫn được đọc vì worker tự bổ sung các trường mới bằng giá trị mặc
định. Nếu muốn chạy mô phỏng lại hoàn toàn từ đầu, cần sao lưu rồi chủ động xóa
state, model và monitoring history cũ trước khi khởi động.

## Test drift an toàn trên EC2

Ba cửa sổ đầu tiên hiển thị:

```text
Baseline MAE: None
Ratio threshold: Disabled
```

Đây là hành vi đúng khi `MIN_BASELINE_WINDOWS=3`. Baseline của cửa sổ hiện tại
chỉ được tính từ các cửa sổ **trước đó**:

```text
Cửa sổ 1: chưa có lịch sử
Cửa sổ 2: mới có 1 MAE
Cửa sổ 3: mới có 2 MAE
Cửa sổ 4: có đủ 3 MAE, baseline bắt đầu xuất hiện
```

Để ép thử nhánh drift mà không train hoặc thay model production:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.drift-test.yml \
  run --rm traffic-drift-worker
```

Kết quả mong đợi:

```text
Forced test drift  : True
DRIFT DETECTED
DRIFT TEST PASSED: drift branch reached.
Candidate training and promotion were skipped.
```

Chế độ này an toàn vì:

- Chỉ chạy một vòng với `RUN_ONCE=true`.
- Không sửa `MAE_THRESHOLD` production.
- Không train Candidate.
- Không promote hoặc ghi đè `best_model.pkl`.
- State và drift history test được ghi trong `/tmp` của container tạm.

Không bật `FORCE_DRIFT_TEST=true` trong file Compose production thông thường.
