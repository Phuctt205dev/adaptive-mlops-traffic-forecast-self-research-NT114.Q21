# Runtime source cho bản phát hành CI/CD

Ba file Python tại đây là source canonical được Git theo dõi cho cloud entrypoint và DAG training. `scripts/cloud_training/prepare_rollout.py`, `scripts/cicd/release.py` và các test sử dụng vị trí này; không phụ thuộc bản copy trong `Deployment_test_on_rke2/runtime/`.

Bản runtime ban đầu lấy từ source local đã được sử dụng trước CI/CD. Cloud entrypoint bổ sung tránh đăng ký router monitor trùng khi nạp toàn bộ backend mới, và health nhận diện release. Các lớp runtime compatibility/SHAP/restore bên dưới vẫn được giữ trên cụm trong giai đoạn chuyển tiếp, không sửa hash gốc để vượt guard.

Hướng vận hành: [CICD_RKE2.md](../../../docs/guides/CICD_RKE2.md).
