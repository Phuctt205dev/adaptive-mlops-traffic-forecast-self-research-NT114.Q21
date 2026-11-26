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

| `real/i94-minneapolis/TrafficVolumeData_I94_Minneapolis_2012_2018.csv` | **Thật**: UCI đầy đủ, thời tiết bổ sung từ ERA5 | 02/10/2012 – 30/09/2018 | 40.575 | `8c333ce2bb92` |
| `real/glasgow/TrafficVolumeData_Glasgow_GD1351_R_2019_2023.csv` | **Thật**: cảm biến Glasgow, thời tiết ERA5, chất lượng không khí CAMS | 01/10/2019 – 30/09/2023 | 33.643 | `b1deb2f05e2f` |
| `real/glasgow/TrafficVolumeData_Glasgow_GD1501_A_2019_2023.csv` | **Thật**: như trên | 01/10/2019 – 30/09/2023 | 33.643 | `774bea21f1df` |
| `real/glasgow/TrafficVolumeData_Glasgow_GD0801_A_2019_2023.csv` | **Thật**: như trên | 01/10/2019 – 30/09/2023 | 33.644 | `a55117df57af` |

Ghi chú:

- Dữ liệu gốc là bộ **UCI Metro Interstate Traffic Volume** (I‑94 hướng Tây,
  trạm ATR 301 của MnDOT, giữa Minneapolis và St Paul): `raw/…` trùng 100%
  33.750 dòng đầu của bộ UCI. Sáu cột `air_pollution_index`, `humidity`,
  `wind_speed`, `wind_direction`, `visibility_in_miles`, `dew_point` đến từ bài
  luyện tập "Predict air pollution index" của HackerEarth, không rõ nguồn;
  `dew_point` trùng `visibility_in_miles` ở 100% số dòng (giá trị 1–9), nên
  **không coi là số đo thật**.
- `raw/…` có 5.161 dòng trùng giờ và nhiều giờ thiếu. `TrafficVolumeData.csv`
  giữ đúng 28.589 giờ có thật (lưu lượng trùng khớp bản gốc) và **điền thêm
  11.938 giờ (29,5%)**; các giờ điền không phải số đo thật.
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

## Dữ liệu thật (`real/`)

Dựng lại từ thư mục gốc của repo (các tệp nguồn đã lưu trong `real/*/source/`):

```bash
python data/real/build_real_datasets.py fetch   # tải lại nguồn (không bắt buộc)
python data/real/build_real_datasets.py build   # dựng 4 tệp 15 cột (ra đúng từng byte, đã kiểm 26/11/2026)
python data/real/build_real_datasets.py check   # chạy bộ kiểm tra dataset của website: cả 4 tệp hợp lệ
```

| Bộ | Nơi đo | Lưu lượng TB (xe/giờ) | Giờ có số đo | Sự kiện thay đổi thật |
| --- | --- | --- | --- | --- |
| I‑94 Minneapolis | 1 trạm, 1 hướng, cao tốc | 3.291 (0 – 7.280) | 77,2% (năm 2014: 4.501 giờ, 2015: 3.593 giờ) | Không có COVID; thiếu dữ liệu dài 2014–2015 |
| Glasgow `GD1351_R` | Cảm biến đường phố | 224 (0 – 733) | 95,9% | COVID: tháng 4/2020 bằng 0,60 lần tháng 1–2/2020 |
| Glasgow `GD1501_A` | Cảm biến đường phố | 186 (0 – 574) | 95,9% | COVID: 0,54 lần |
| Glasgow `GD0801_A` | Cảm biến đường phố | 155 (0 – 467) | 96,0% | COVID: 0,70 lần |

Ba cảm biến Glasgow là ba cảm biến có lưu lượng trung bình cao nhất, đã qua đủ
các bước lọc của bộ dữ liệu gốc (`status.csv`). Muốn thêm cảm biến khác: thêm mã
vào `GLASGOW_SENSORS` trong script rồi chạy `fetch` và `build`.

**Giờ thiếu không được điền:** chỉ có dòng cho giờ có số đo; pipeline train tự
lấp chuỗi giờ, đánh dấu giờ thiếu là chưa quan sát và không dùng làm nhãn.

**Cách điền 15 cột**

| Cột | I‑94 Minneapolis | Glasgow |
| --- | --- | --- |
| `date_time` | Giờ địa phương America/Chicago (có đổi giờ mùa hè; suy ra vì nhiệt độ UCI khớp ERA5 tốt nhất ở UTC−5 vào mùa hè, sai lệch 1,34 K so với 1,64 K ở UTC−6) | Giờ **UTC** (giờ cao điểm chiều sớm hơn 1 giờ vào mùa hè) |
| `traffic_volume` | UCI; bỏ 7.629 dòng trùng giờ (cùng số xe, chỉ khác nhãn thời tiết), giữ dòng đầu | Cột `flow` của Zenodo |
| `temperature` | UCI (K); 10 giờ ghi 0 K được thay bằng ERA5 | ERA5 `temperature_2m` (đổi sang K) |
| `rain_p_h` | UCI (mm/giờ); 1 giờ ghi 9.831,3 mm được thay bằng ERA5 | ERA5 `rain` |
| `snow_p_h` | UCI | ERA5 `snowfall` (cm → mm) |
| `clouds_all`, `weather_type`, `weather_description` | UCI | ERA5 `cloud_cover`; `weather_code` (mã WMO) đổi sang nhãn cùng kiểu UCI |
| `humidity`, `dew_point` (K), `wind_speed` (mph), `wind_direction` | ERA5 | ERA5 |
| `is_holiday` | Ngày lễ Mỹ và Hội chợ bang Minnesota của UCI, đánh dấu cả ngày (53 ngày) | Ngày nghỉ chính thức của Scotland (gov.uk), theo ngày giờ địa phương (39 ngày) |
| `air_pollution_index` | **0 = không có dữ liệu** (không có nguồn thật) | CAMS `european_aqi` (thang EAQI châu Âu, 8–64; khác thang 10–300 của tệp HackerEarth) |
| `visibility_in_miles` | **0 = không có dữ liệu** | **0 = không có dữ liệu** (ERA5 trên Open‑Meteo không có tầm nhìn) |

Cột "0 = không có dữ liệu" là hằng số nên model không học được gì từ cột đó.
Thời tiết ERA5 là dữ liệu tái phân tích ô lưới khoảng 25 km (một điểm cho cả
hành lang I‑94: 44,96; −93,18; Glasgow: trung bình vị trí ba cảm biến).

**Lưu ý khi tải lên website:** Model Quality và kiểm tra drift tính theo thời
điểm hiện tại, nên với dữ liệu lịch sử (2012–2018, 2019–2023) hai chức năng này
sẽ không có điểm để so. Các bộ này dùng chủ yếu cho thực nghiệm offline.

**Nguồn và giấy phép**

| Nguồn | Giấy phép | Trích dẫn |
| --- | --- | --- |
| [UCI Metro Interstate Traffic Volume](https://archive.ics.uci.edu/dataset/492/metro+interstate+traffic+volume) | CC BY 4.0 | Hogue, J. (2019). Metro Interstate Traffic Volume. UCI Machine Learning Repository. https://doi.org/10.24432/C5X60B |
| [High-resolution traffic flow data in Glasgow](https://zenodo.org/records/14537113) (v2, MD5 của tệp zip `fe7bcce6abc8b69046cbff28cf466991`) | Open Government Licence v3.0 | Yue, L., Zhao, Q., Wang, M. (2024). Zenodo. https://doi.org/10.5281/zenodo.14537113 |
| [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api) (ERA5) và [Air Quality API](https://open-meteo.com/en/docs/air-quality-api) (CAMS European) | CC BY 4.0 (theo [điều khoản](https://open-meteo.com/en/terms)) | Zippenfenig, P. (2023). Open-Meteo.com Weather API. https://doi.org/10.5281/ZENODO.7970649; ERA5: Copernicus Climate Change Service; CAMS: Copernicus Atmosphere Monitoring Service |
| [UK bank holidays](https://www.gov.uk/bank-holidays) (bản JSON `https://www.gov.uk/bank-holidays.json`, phần `scotland`) | Open Government Licence v3.0 (chân trang GOV.UK) | GOV.UK, Government Digital Service |
