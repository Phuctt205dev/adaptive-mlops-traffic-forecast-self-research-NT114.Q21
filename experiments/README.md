# Thực nghiệm đánh giá hệ thống

> **Cập nhật:** 26/11/2026 (múi giờ Asia/Saigon). Thư mục nằm ở gốc repo (`experiments/`), có trong Git để người khác chạy lại được; không đóng vào image (`.dockerignore`). Nhật ký làm việc vẫn ở `My-worklog/` (chỉ để ghi nhật ký).
> **Thư mục này trả lời:** hệ thống dự báo tốt cỡ nào, khi nào nên retrain, và chạy nhanh/ổn cỡ nào, bằng **con số đo được**.
> **Đọc cùng:** handoff [2026-11-25-system-evaluation.md](../My-worklog/hand-offs/2026-11-25-system-evaluation.md) (nhật ký hành động chi tiết) · [project_context.md](../My-worklog/project_context.md) (hiện trạng hệ thống).

Mỗi file là **một câu hỏi**. Người chưa biết dự án chỉ cần đọc phần "Kết luận ngắn" ở đầu mỗi file; người viết báo cáo lấy bảng và hình ở phần "Kết quả".

## Lộ trình theo giai đoạn

Làm lần lượt từng giai đoạn; mỗi giai đoạn xong có một kết quả xem được và được người dùng xác nhận rồi mới sang giai đoạn sau.

| Giai đoạn | Câu hỏi | Trạng thái | File |
| --- | --- | --- | --- |
| 1 | Dữ liệu dự án đang dùng ra sao? Dọn thư mục `data/` | **Hoàn thành** (người dùng xác nhận 26/11) | [E0-dataset.md](E0-dataset.md) (phần A) |
| 2 | Có bộ dữ liệu thật nào gần với dự án? | **Hoàn thành** (người dùng: "cũng ổn rồi", 26/11) | [E0-dataset.md](E0-dataset.md) (phần B) |
| 3 | Chỉ số nào đánh giá khả năng dự đoán tốt nhất? | **Hoàn thành** (người dùng xác nhận 26/11): chọn model = MAE; hiển thị = WAPE + MAE + so với cách đoán tuần trước; theo dõi = họ MAE/WAPE (kiểm ở E3). Kết quả train offline ở `outputs/e1/`, hình ở `figures/` | Báo cáo [E1-chi-so-danh-gia.md](E1-chi-so-danh-gia.md); notebook [notebooks/E1-chi-so-danh-gia.ipynb](notebooks/E1-chi-so-danh-gia.ipynb) |
| 4 | Tab Model Quality nên hiển thị sai số thế nào? | **Đã phát hành** (26/11, PR #33, kiểm sau CD đạt): tab hiện WAPE + MAE + so với cùng giờ tuần trước, nhãn Low traffic; chờ người dùng xác nhận | [E2-model-quality.md](E2-model-quality.md); script [scripts/e2_backend_replay.py](scripts/e2_backend_replay.py) |
| 5 | Khi nào nên retrain? Tín hiệu drift nào tốt nhất? | **Đang làm**: bước 5.1 xong (27/11) — cách phát hiện drift hiện tại báo ≈ 98% kể cả khi chắc chắn không có drift | Notebook [notebooks/E3-drift-retrain.ipynb](notebooks/E3-drift-retrain.ipynb) (dựng bằng `scripts/build_e3_notebook.py`); báo cáo `E3-drift-retrain.md` (viết ở 5.5) |
| 6 | Hệ thống chạy nhanh và ổn cỡ nào? | Chưa làm | `E4-hieu-nang.md` |
| 7 | Tổng hợp cho báo cáo | Chưa làm | `00-tong-hop.md` |

**Cách học theo notebook:** mở tệp `.ipynb` trong `notebooks/` bằng VS Code (đã cài tiện ích Jupyter), chọn kernel `.venv`, chạy từng ô bằng Shift + Enter. Mỗi phần có: *Đang làm gì* → *Đoán trước* → ô code → *Đọc kết quả*. Khung tư duy chung ở [PHUONG-PHAP.md](PHUONG-PHAP.md). Notebook được tạo lại bằng `scripts/build_e1_notebook.py`.

Trạng thái dùng bốn mức chung của nhật ký: **đã viết** (có script/kế hoạch) → **đã chạy** (có số) → **đã kiểm** (chạy lại cho cùng kết quả, đã soát lỗi) → **người dùng xác nhận**.

## Quy ước để số liệu dùng được trong báo cáo

1. **Ghi rõ loại dữ liệu** ở mọi bảng: *synthetic* (do dự án tạo), *thật* (bộ dữ liệu công khai) hay *mô phỏng drift* (dữ liệu thật hoặc synthetic đã được cố ý thay đổi). Kết quả trên synthetic không phải kết luận về giao thông thật.
2. **Một lần đo không phải kết luận.** Số đo thời gian/tài nguyên cần ≥ 5 lần cùng điều kiện, ghi *trung bình ± độ lệch chuẩn* và *min–max*. Chỉ số sai số ghi rõ dataset, khoảng thời gian, cách chia train/test.
3. **So với một mốc.** Mỗi chỉ số model đặt cạnh ít nhất một cách dự đoán ngây thơ (ví dụ "lấy đúng giờ đó tuần trước") để biết model có thật sự hơn không.
4. **Giữ điều kiện giống hệ thống thật:** cùng cách chia theo thời gian, cùng feature, cùng tham số model như pipeline đang chạy; khác ở đâu thì ghi ra.
5. **Có thể chạy lại:** ghi đường dẫn script, phiên bản thư viện, seed. Script thực nghiệm đặt cạnh file md (thư mục `scripts/` trong thư mục này); dữ liệu tải về đặt ngoài thư mục mã.
6. **Nguồn tham khảo** chỉ ghi tài liệu đã mở đọc (theo `ai_rule.md` mục 9.3), có cột "dùng cho".
7. Ngày ghi dd/mm, giờ Asia/Saigon.

## Mẫu một file thực nghiệm

```markdown
# E? — <Câu hỏi bằng lời thường>

> **Cập nhật:** dd/mm/2026 · **Trạng thái:** đã viết / đã chạy / đã kiểm / người dùng xác nhận
> **Kết luận ngắn:** 2–3 câu, người ngoài đọc là hiểu.

## 1. Vì sao cần thực nghiệm này
Bối cảnh trong dự án, ghi chú của GVHD liên quan.

## 2. Dữ liệu
| Dataset | Loại (thật/synthetic/mô phỏng) | Khoảng thời gian | Số dòng | Ghi chú |

## 3. Cách làm
Các bước đánh số; chỉ số dùng và ý nghĩa bằng lời thường; điều kiện giữ giống hệ thống.

## 4. Kết quả
Bảng số (đơn vị rõ ràng), hình (đặt trong `figures/`), mỗi bảng/hình một câu "đọc thế nào".

## 5. Kết luận và đề xuất
Trả lời thẳng câu hỏi ở tiêu đề; đề xuất thay đổi cho hệ thống (nếu có).

## 6. Giới hạn
Điều chưa đo, giả định, khác biệt với hệ thống thật.

## 7. Cách chạy lại
Lệnh, script, phiên bản thư viện, seed.

## 8. Nguồn tham khảo
| Tài liệu và liên kết | Tác giả/tổ chức | Dùng cho |
```

## Từ điển nhanh các chỉ số (lời thường)

Ví dụ chung: giờ đó thực tế có **5.000 xe**, model đoán **4.800 xe** → lệch **200 xe**.

| Chỉ số | Nói đơn giản | Điểm mạnh | Điểm yếu |
| --- | --- | --- | --- |
| **MAE** | Trung bình mỗi giờ đoán lệch bao nhiêu xe. | Dễ hiểu, cùng đơn vị xe/giờ. | Không cho biết lệch thế là nhiều hay ít (lệch 200 xe lúc cao điểm khác hẳn lúc 3 giờ sáng). |
| **RMSE** | Giống MAE nhưng phạt nặng những giờ đoán lệch rất xa. | Lộ ra các lần đoán hụt lớn (ví dụ hụt giờ cao điểm). | Khó giải thích hơn MAE; một vài giờ bất thường kéo số lên mạnh. |
| **MAPE** | Trung bình mỗi giờ lệch bao nhiêu **phần trăm**. | Có phần trăm, dễ so giữa các vùng. | Giờ rất ít xe (gần 0) làm phần trăm "nổ" (lệch 50 xe khi thực tế 10 xe = 500%). |
| **WAPE** | Tổng số xe đoán lệch chia tổng số xe thực tế, ra phần trăm. | Có phần trăm mà không bị "nổ" như MAPE; đọc là "sai khoảng x% lưu lượng". | Giờ đông xe chiếm trọng số lớn hơn giờ vắng. |
| **R²** | Model giải thích được bao nhiêu phần dao động của lưu lượng. | Một con số 0–1 quen thuộc. | Dễ cao "ảo" vì giao thông có nhịp ngày–đêm rất đều: đoán theo nhịp đó đã được R² cao. |
| **MASE / skill** | So model với cách đoán ngây thơ (ví dụ lấy đúng giờ đó tuần trước). Dưới 1 nghĩa là model tốt hơn cách ngây thơ. | Trả lời câu "model có đáng dùng không". | Phải chọn cách đoán ngây thơ phù hợp và ghi rõ. |

Các chỉ số phát hiện drift (PSI, JS, KS, CUSUM, Page-Hinkley) sẽ được giải thích trong `E3-drift-retrain.md`. Định nghĩa chính thức và nguồn sẽ bổ sung khi làm E1/E3 (chưa tra cứu tài liệu ngoài ở thời điểm tạo file này).
