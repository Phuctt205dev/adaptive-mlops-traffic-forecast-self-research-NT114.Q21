# Dữ liệu của dự án

Thư mục này chỉ chứa **dataset**. Tài liệu của đồ án chuyên ngành trước đây đã
chuyển sang `docs/archive/dacn/`. Thư mục `data/` không được đóng vào image
(`.dockerignore`); hệ thống đang chạy đọc dataset từ kho đối tượng sau khi admin
tải lên, không đọc trực tiếp từ đây.

Mọi tệp CSV có cùng 15 cột: `date_time`, `is_holiday`, `air_pollution_index`,
`humidity`, `wind_speed`, `wind_direction`, `visibility_in_miles`, `dew_point`,
`temperature` (Kelvin), `rain_p_h`, `snow_p_h`, `clouds_all`, `weather_type`,
`weather_description`, `traffic_volume` (xe/giờ).

## Danh sách

| Tệp | Loại | Khoảng thời gian | Số dòng | SHA-256 (12 ký tự đầu) |
| --- | --- | --- | --- | --- |
| `raw/TrafficVolumeData_original_2012_2017.csv` | Dữ liệu thật, bản tải về gốc (giao thông và thời tiết ở Mỹ) | 02/10/2012 – 17/05/2017 | 33.750 | `e751ce85ef28` |
| `TrafficVolumeData.csv` | Dữ liệu thật đã làm sạch: mỗi giờ một dòng, không trùng | 02/10/2012 – 17/05/2017 | 40.527 | `7c4e4b3f5a54` |
| `synthetic/TrafficVolumeData_HCM_2015_2030_synthetic.csv` | **Giả lập**, sinh bằng script từ `TrafficVolumeData.csv` | 01/01/2015 – 31/12/2030 | 140.256 | `80a35e9b65a6` |
| `synthetic/TrafficVolumeData_Hanoi_2026_2030_synthetic.csv` | **Giả lập**, sinh bằng script từ tệp Hồ Chí Minh | 01/01/2026 – 31/12/2030 | 43.824 | `5d8a3f3711e7` |

Ghi chú:

- `raw/…` có 5.161 dòng trùng giờ và nhiều giờ thiếu. `TrafficVolumeData.csv`
  giữ đúng 28.589 giờ có thật (lưu lượng trùng khớp bản gốc) và **điền thêm
  11.938 giờ (29,5%)**; các giờ điền không phải số đo thật.
- Ngày lễ trong dữ liệu gốc là ngày lễ Mỹ. Thành phố/tuyến đường đo chưa được
  kiểm chứng bằng nguồn công bố.
- Mã băm tính trên tệp cuối dòng LF (`.gitattributes` ghim `*.csv` về LF nên
  mọi máy lấy ra giống nhau). Bản `raw/` trùng từng byte với tệp trong
  `archive.zip` tải về trước đây (đã bỏ vì trùng).
- Hai tệp giả lập là đúng hai tệp đang được dùng cho vùng Hồ Chí Minh và Hà Nội
  trên hệ thống (mã băm khớp với bản ghi dataset).

## Dữ liệu giả lập (`synthetic/`)

Chạy lại từ thư mục gốc của repo (tạo lại đúng từng byte, đã kiểm 25/11/2026):

```bash
python data/synthetic/generate_hcm_synthetic.py    # seed 20261002, đọc data/TrafficVolumeData.csv
python data/synthetic/generate_hanoi_synthetic.py  # seed 20261003, đọc tệp Hồ Chí Minh
```

Cách sinh (chi tiết trong hai tệp `*.README.md` và mã nguồn):

- **Hồ Chí Minh:** với mỗi ngày từ 2015 đến 2030, lấy ngẫu nhiên **một ngày
  nguyên** của dữ liệu Mỹ cùng tháng và cùng thứ, nhân nhiễu nhỏ, tăng 1,2%/năm,
  giảm 6% khi mưa và 28% vào 4 ngày lễ dương lịch (không có Tết). Thời tiết
  được thay bằng công thức khí hậu nhiệt đới.
- **Hà Nội:** lấy cùng các giờ 2026–2030 từ tệp Hồ Chí Minh, thêm biến động
  riêng và thời tiết bốn mùa giả định.

**Chỉ dùng để kiểm thử tích hợp và tải**, đúng như README gốc của hai tệp.
Không dùng để kết luận về giao thông thật của Hà Nội/Hồ Chí Minh hay về độ chính
xác của model, vì:

| Đo trên dữ liệu (25/11/2026) | Dữ liệu thật | Hồ Chí Minh | Hà Nội |
| --- | --- | --- | --- |
| Hôm qua đông/vắng hơn thường lệ thì hôm nay cũng vậy (tương quan) | 0,55 – 0,59 | 0,36 | 0,17 |
| Lưu lượng giờ mưa so với giờ không mưa | 1,02 – 1,07 | 1,22 | 1,28 |

Các ngày bị ghép ngẫu nhiên nên mối liên hệ giữa các ngày liên tiếp yếu đi rõ
rệt. Mưa chỉ được đặt vào buổi chiều tối (13–21 giờ, trùng giờ cao điểm), nên
dữ liệu cho thấy "mưa thì đông xe hơn", ngược với giả định giảm 6% khi sinh.
