# E0 — Dùng bộ dữ liệu nào để thử nghiệm cho đáng tin?

> **Cập nhật:** 25/11/2026 · **Trạng thái:** phần A (dataset đang dùng) **đã chạy**; phần B (dataset công khai) **đã tra cứu và kiểm khả thi**, chờ người dùng chọn.
> **Kết luận ngắn (phần A):** hai dataset Hà Nội và Hồ Chí Minh đều là dữ liệu do AI tạo sinh từ một bộ dữ liệu giao thông của Mỹ (2012–2017). Chúng đầy đủ từng giờ và đúng định dạng, nhưng "quá đều": lưu lượng chỉ tăng chậm khoảng 1%/năm, thời tiết gần như giống hệt nhau mỗi năm, không có sự kiện bất thường nào. Vì vậy chúng dùng được để kiểm tra hệ thống chạy đúng, nhưng **chưa đủ để đánh giá phát hiện drift** (E3); cần thêm kịch bản drift mô phỏng và ít nhất một dataset thật.

## 1. Vì sao cần thực nghiệm này

Mọi con số đánh giá (sai số, phát hiện drift) đều phụ thuộc dữ liệu. Nếu dữ liệu không có thay đổi nào thì bộ phát hiện drift "không báo gì" cũng trông như đúng. Phần A mô tả dữ liệu dự án đang dùng; phần B tìm dữ liệu thật gần với dự án (ghi chú của GVHD: đánh giá định lượng, dataset gần gũi).

## 2. Dữ liệu (phần A)

| Dataset | Loại | Khoảng thời gian | Số dòng | Nơi lưu |
| --- | --- | --- | --- | --- |
| Gốc `data/TrafficVolumeData.csv` | Thật, đã làm sạch (29,5% số giờ là giá trị điền thêm) | 02/10/2012 – 17/05/2017 | 40.527 | Repo |
| Gốc thô `data/raw/TrafficVolumeData_original_2012_2017.csv` | Thật, bản tải về | 02/10/2012 – 17/05/2017 | 33.750 (5.161 dòng trùng giờ) | Repo |
| Hà Nội `TrafficVolumeData_Hanoi_2026_2030_synthetic.csv` | Synthetic (AI tạo sinh từ dataset gốc) | 01/01/2026 – 31/12/2030 | 43.824 | Hệ thống (MinIO), dataset `a2297751` |
| Hồ Chí Minh `TrafficVolumeData_HCM_2015_2030_synthetic.csv` | Synthetic (AI tạo sinh từ dataset gốc) | 01/01/2015 – 31/12/2030 | 140.256 | Hệ thống (MinIO), dataset `877d13f5` |

Cả bốn tệp có cùng 15 cột: `date_time`, `is_holiday`, 10 cột thời tiết/môi trường, `weather_type`, `weather_description`, `traffic_volume`. Nhiệt độ ghi theo độ Kelvin (297,4 K ≈ 24,3 °C).

## 3. Cách làm (phần A)

1. Tải bản sao **chỉ đọc** hai dataset thành phố từ hệ thống; đối chiếu mã băm SHA-256 với bản ghi trong DB (khớp cả hai).
2. Đo bằng pandas: đủ giờ hay không, trùng giờ, giá trị thiếu, lưu lượng trung bình theo năm/giờ/thứ, ngày lễ, thời tiết.
3. So dataset gốc thô với dataset gốc đã làm sạch: ghép theo giờ để biết bao nhiêu giờ là dữ liệu có thật.

## 4. Kết quả (phần A)

**Bảng 1. Tổng quan lưu lượng (xe/giờ)**

| Chỉ số | Gốc (Mỹ) | Hà Nội | Hồ Chí Minh |
| --- | --- | --- | --- |
| Đủ từng giờ, không trùng | Có | Có | Có |
| Lưu lượng trung bình | 3.297 | 3.878 (+17,6%) | 3.597 (+9,1%) |
| Độ lệch chuẩn | 1.980 | 2.374 | 2.173 |
| Thấp nhất / cao nhất | 0 / 7.280 | 0 / 9.882 | 0 / 9.026 |
| Tỉ lệ giờ dưới 1.000 xe | 22,1% | 19,9% | 20,8% |
| Giờ vắng nhất (trung bình) | 3 giờ: 371 | 3 giờ: 430 | 3 giờ: 405 |
| Giờ đông nhất (trung bình) | 16 giờ: 5.693 | 16 giờ: 6.551 | 16 giờ: 6.187 |
| Thứ Hai … Chủ nhật | 3.347 · 3.567 · 3.608 · 3.653 · 3.665 · 2.824 · 2.411 | 4.048 · 4.232 · 4.241 · 4.325 · 4.310 · 3.215 · 2.777 | 3.707 · 3.889 · 3.930 · 4.004 · 3.969 · 3.058 · 2.625 |

*Đọc thế nào:* hai thành phố có cùng "nhịp" với dữ liệu gốc (vắng nhất 3 giờ, đông nhất 16 giờ, cuối tuần thấp hơn), chỉ được nâng mức lên. Khoảng 1/5 số giờ có dưới 1.000 xe, nên chỉ số tính theo phần trăm từng giờ (MAPE) sẽ bị những giờ này kéo lệch (xem E1).

**Bảng 2. Lưu lượng trung bình theo năm (xe/giờ)**

| Năm | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 | 2027 | 2028 | 2029 | 2030 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Hồ Chí Minh | 3.302 | 3.358 | 3.376 | 3.444 | 3.454 | 3.517 | 3.544 | 3.575 | 3.614 | 3.657 | 3.665 | 3.746 | 3.778 | 3.795 | 3.847 | 3.888 |
| Hà Nội | | | | | | | | | | | | 3.805 | 3.854 | 3.865 | 3.909 | 3.957 |

*Đọc thế nào:* lưu lượng tăng đều khoảng **1%/năm** ở cả hai thành phố (Hồ Chí Minh 3.302 → 3.888 trong 15 năm; Hà Nội 3.805 → 3.957 trong 4 năm). Năm 2020 không giảm như giai đoạn COVID-19 ngoài đời: dữ liệu không chứa sự kiện bất thường.

**Bảng 3. Thời tiết và ngày lễ**

| Chỉ số | Gốc (Mỹ) | Hà Nội | Hồ Chí Minh |
| --- | --- | --- | --- |
| Nhiệt độ trung bình theo năm | thay đổi 274,9 – 282,2 K giữa các năm | **297,4 K mọi năm** | **301,1 – 301,2 K mọi năm** |
| Loại thời tiết phổ biến | Clear 41,3%, Clouds 36,5%, Mist 6,9%, Rain 5,9% | Clouds 47,0%, Clear 39,4%, Mist 6,9%, Rain 2,2% | Clouds 45,7%, Clear 40,3%, Mist 6,9%, Rain 2,5% |
| Ngày lễ được đánh dấu | Ngày lễ Mỹ (Columbus Day, Thanksgiving Day, Veterans Day, New Years Day…): 38 ngày, ngày nào cũng đánh dấu giờ 0, thêm 68 giờ rải rác khác (tổng 106 giờ) | 4 ngày lễ cố định của Việt Nam (Tết Dương lịch, 30/4, 1/5, 2/9), đánh dấu cả 24 giờ | Như Hà Nội |

*Đọc thế nào:* thời tiết trong dữ liệu synthetic **lặp lại y hệt mỗi năm** (nhiệt độ trung bình năm giống nhau tới 0,1 K). Không có **Tết Nguyên đán** và các ngày lễ âm lịch, vốn làm giao thông Việt Nam thay đổi mạnh nhất trong năm.

## 5. Kết luận và đề xuất (phần A)

1. Hai dataset thành phố **dùng tốt cho việc kiểm tra hệ thống chạy đúng** (đủ giờ, đúng định dạng, có nhịp ngày/tuần rõ ràng) và cho E1 (so sánh chỉ số).
2. Chúng **chỉ có một kiểu thay đổi**: tăng chậm, đều. Bộ phát hiện drift hiện tại chỉ nhìn thời tiết, mà thời tiết lại lặp y hệt mỗi năm, nên trên dữ liệu này nó gần như không có gì để phát hiện ngoài khác biệt giữa các mùa. Muốn đánh giá "khi nào nên retrain" (E3) cần:
   - **kịch bản drift mô phỏng** có đáp án (biết trước drift bắt đầu lúc nào, mạnh cỡ nào), dựng trên chính các dataset này;
   - **ít nhất một dataset thật** có sự kiện thay đổi thật (phần B).
3. Khi báo cáo, ghi rõ: kết quả trên hai dataset này là kết quả trên **dữ liệu synthetic**, không phải giao thông thật của Hà Nội hay Hồ Chí Minh.

### Cách hai tệp được sinh ra và vì sao GVHD thấy "không hợp lý"

Hai script sinh nằm trong dự án ĐACN cũ, nay lưu ở `data/synthetic/` (chạy lại ra đúng từng byte tệp đang dùng trên hệ thống):

- **Hồ Chí Minh:** với mỗi ngày 2015–2030, chọn ngẫu nhiên **một ngày nguyên của dữ liệu Mỹ** cùng tháng và cùng thứ, nhân nhiễu nhỏ, tăng 1,2%/năm, giảm 6% khi mưa, giảm 28% vào 4 ngày lễ dương lịch; thời tiết được thay bằng công thức (mưa chỉ đặt từ 13 đến 21 giờ).
- **Hà Nội:** lấy lại các giờ 2026–2030 của tệp Hồ Chí Minh, thêm biến động và thời tiết bốn mùa giả định.

README gốc của chính hai tệp ghi: chỉ dùng để kiểm thử tích hợp và tải, không dùng để kết luận về độ chính xác của model.

**Bảng 4. Bằng chứng định lượng**

| Đo | Dữ liệu thật (gốc) | Hồ Chí Minh | Hà Nội | Ý nghĩa |
| --- | --- | --- | --- | --- |
| Hôm qua đông/vắng hơn thường lệ thì hôm nay cũng vậy (tương quan, sau khi bỏ nhịp thứ–giờ) | 0,55 – 0,59 | 0,36 | 0,17 | Các ngày bị ghép ngẫu nhiên nên "trí nhớ" giữa các ngày yếu đi; model dùng lag 24 giờ, 168 giờ học trên quan hệ giả. |
| Lưu lượng giờ mưa ÷ giờ không mưa | 1,02 – 1,07 | 1,22 | 1,28 | Mưa bị đặt vào giờ cao điểm nên dữ liệu "nói" mưa thì đông xe hơn, ngược với giả định khi sinh. |

## 6. Giới hạn

- Hai tương quan ở Bảng 4 là phép đo đơn giản (một cách tính), đủ để thấy khác biệt lớn, chưa phải phân tích thống kê đầy đủ.
- Dataset gốc được người dùng gọi là "dataset Chicago"; phần B đã xác định đó là bộ UCI Metro Interstate (I‑94, Minneapolis–St Paul).
- 29,5% số giờ của dataset gốc đã làm sạch là giá trị điền thêm, không phải đo thật; cách điền chưa xác định.

## 7. Cách chạy lại

- Bản sao hai dataset: đọc qua pod API bằng `backend.app.services.storage.get_s3_client()` theo `Dataset.storage_uri`; kiểm SHA-256 với `Dataset.sha256` (Hà Nội `5d8a3f3711e7…`, Hồ Chí Minh `80a35e9b65a6…`).
- Thống kê: pandas 3.0.2 trong `.venv` (Python 3.11.9), các phép `groupby` theo năm/giờ/thứ trên `traffic_volume`.
- Script sẽ được chuyển vào `experiments/scripts/` khi chốt cấu trúc thư mục `data/` (giai đoạn 1).

## 8. Nguồn tham khảo

| Tài liệu và liên kết | Tác giả/tổ chức | Dùng cho |
| --- | --- | --- |
| Không dùng tài liệu tham khảo ngoài (phần A chỉ đo dữ liệu của dự án) | | |

---

## Phần B — Dataset công khai gần với dự án (giai đoạn 2)

> **Trạng thái:** **đã chạy và đã kiểm** (26/11). Người dùng chốt: chỉ dùng nguồn tải được và dùng được, lưu gọn trong `data/`. Đã dựng 4 tệp thật đủ 15 cột trong `data/real/` (I‑94 Minneapolis và ba cảm biến Glasgow), cả 4 đạt bộ kiểm tra dataset của website; MnDOT và Kaggle Hồ Chí Minh bị loại (không tải/không dùng được). Chi tiết dựng ở mục B6.
> **Kết luận ngắn (phần B):** dataset gốc của dự án thực ra là bộ **Metro Interstate Traffic Volume** (đường cao tốc I‑94, Minneapolis–St Paul, Mỹ), không phải Chicago: 33.750 dòng của dự án trùng 100% với bộ UCI. Không tìm được dữ liệu đếm xe theo giờ công khai của Việt Nam. Đề xuất dùng hai bộ thật: **UCI Metro Interstate đầy đủ** (2012–2018, cùng nguồn với dữ liệu gốc) và **Glasgow** (2019–2023, có giai đoạn COVID làm lưu lượng giảm còn khoảng một nửa). Thời tiết lấy từ dữ liệu lịch sử thật (Open‑Meteo/ERA5).

Tiêu chí: dữ liệu **theo giờ**, **≥ 2 năm**, có hoặc ghép được **thời tiết lịch sử** và **ngày lễ**, **giấy phép cho phép dùng** trong nghiên cứu, chuyển được về 15 cột; ưu tiên có **sự kiện thay đổi thật** để làm đáp án cho E3; ưu tiên Việt Nam nếu có.

### B1. Nguồn gốc thật của dataset đang dùng

| Kiểm tra | Kết quả |
| --- | --- |
| Bộ UCI "Metro Interstate Traffic Volume" (tải về 25/11) | 48.204 dòng, 02/10/2012 09:00 – 30/09/2018 23:00, 9 cột; trạm ATR 301 của MnDOT, I‑94 hướng Tây, "roughly midway between Minneapolis and St Paul"; CC BY 4.0. |
| So 33.750 dòng đầu của UCI với `data/raw/…` của dự án | Ngày giờ, `traffic_volume`, nhiệt độ, `clouds_all`, `weather_type`/`weather_main`, `weather_description` **trùng 100%**. |
| Sáu cột thêm (`air_pollution_index`, `humidity`, `wind_speed`, `wind_direction`, `visibility_in_miles`, `dew_point`) | Có trong bài luyện tập "Predict air pollution index" của HackerEarth (`train.csv` 33.751 dòng kể cả tiêu đề = đúng số dòng của dự án). Trang **không nêu nguồn gốc và giấy phép** của các cột này → không kiểm chứng được là số đo thật. |
| Phần UCI mà dự án chưa dùng | 14.454 dòng từ 17/05/2017 đến 30/09/2018. |
| Độ đầy đủ của UCI (giờ không trùng mỗi năm) | 2012: 2.103 · 2013: 7.294 · **2014: 4.501 · 2015: 3.593** · 2016: 7.838 · 2017: 8.713 · 2018: 6.533 (một năm có 8.760 giờ). |

*Đọc thế nào:* "dataset Chicago" là cách gọi chưa đúng. Khi viết báo cáo nên trích đúng nguồn UCI (Hogue, 2019) và nói rõ sáu cột thêm không rõ nguồn.

### B2. Các ứng viên

| # | Dataset | Ở đâu, khi nào | Độ phân giải | Có sự kiện thay đổi thật? | Giấy phép | Kiểm khả thi (25/11) | Đánh giá |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | **UCI Metro Interstate Traffic Volume** (đầy đủ) | I‑94 Minneapolis–St Paul, 10/2012 – 09/2018 | Theo giờ, 1 trạm 1 hướng; lưu lượng TB ≈ 3.300 xe/giờ | Có khoảng trống lớn 2014–2015; không có COVID | CC BY 4.0 | Tải được, đã so khớp (B1) | **Nên dùng**: thật, cùng nguồn với dữ liệu đang có, ít phải sửa |
| 2 | **Glasgow traffic flow** (Zenodo, Yue, Zhao, Wang) | Thành phố Glasgow (Anh), 10/2019 – 09/2023 | Theo giờ, **470 cảm biến**; lưu lượng TB mỗi cảm biến 6,7–224 xe/giờ (trung vị 47) | **Có: COVID** — lưu lượng tháng 4/2020 bằng trung vị **0,50** (khoảng 0,13–2,07 tùy cảm biến) so với tháng 1–2/2020 | Open Government Licence v3.0 | Tải được 55,6 MB (MD5 khớp), mỗi cảm biến đủ ≈ 96% số giờ | **Nên dùng cho E3**: drift thật, có mốc thời gian rõ; đường phố nên lưu lượng nhỏ hơn I‑94 |
| 3 | MnDOT ATR/WIM hourly volume | Minnesota, 2017–2024 (Excel theo năm) | Theo giờ, nhiều trạm; trang không nói có tách hướng không | Có thể có COVID 2020 (chưa kiểm) | Không có giấy phép mở, chỉ tuyên bố miễn trừ "as is" | **Máy không kết nối được** máy chủ tải file (hết thời gian chờ); chưa biết có trạm 301 không | Dự phòng: rất hay nếu có trạm 301 (nối tiếp dữ liệu gốc tới 2024), cần người dùng thử tải bằng trình duyệt |
| 4 | "Traffic Flow data in Ho Chi Minh City" (Kaggle) | TP. Hồ Chí Minh | Theo kết quả tìm kiếm: mức độ phục vụ (LOS) theo 30 phút, không phải số xe | — | Chưa rõ | Trang Kaggle không hiển thị nội dung khi đọc tự động; **chưa kiểm chứng** | Không phù hợp nếu đúng là LOS (hệ thống dự báo số xe) |
| — | Dữ liệu đếm xe theo giờ công khai ở Việt Nam | — | — | — | — | Tìm kiếm không thấy (chỉ có số đăng ký xe theo tháng, nghiên cứu tiếng ồn 2009, dữ liệu thương mại) | Không có |

### B3. Điền đủ 15 cột cho dữ liệu thật

Hệ thống bắt buộc 15 cột và **không cho ô trống** ở cột số (bộ kiểm tra `dataset_numeric_invalid`). Thời tiết lịch sử thật lấy từ **Open‑Meteo Historical Weather API** (ERA5, theo giờ từ 1940, CC BY 4.0, miễn phí cho nghiên cứu phi thương mại):

| Cột của hệ thống | Bộ 1 (UCI) | Bộ 2 (Glasgow) |
| --- | --- | --- |
| `temperature`, `rain_p_h`, `snow_p_h`, `clouds_all`, `weather_type`, `weather_description` | Có sẵn trong UCI (số đo gốc) | Open‑Meteo: `temperature_2m` (đổi sang K), `rain`, `snowfall` (cm → mm), `cloud_cover`, `weather_code` (đổi sang chữ) |
| `humidity`, `dew_point`, `wind_speed`, `wind_direction` | Open‑Meteo: `relative_humidity_2m`, `dew_point_2m`, `wind_speed_10m` (mph), `wind_direction_10m` | Như bên trái |
| `is_holiday` | Có sẵn trong UCI (ngày lễ Mỹ) | Lịch ngày lễ Scotland (cần nguồn chính thức) |
| `air_pollution_index` | **Không có nguồn thật** cho Mỹ giai đoạn này → giá trị giữ chỗ, ghi rõ | Có thể lấy `european_aqi` của CAMS (châu Âu, tái phân tích từ 2013); **chưa thử** API có trả dữ liệu lịch sử không |
| `visibility_in_miles` | **Open‑Meteo lịch sử không có** → giá trị giữ chỗ, ghi rõ | Như bên trái |

Giá trị giữ chỗ (một hằng số) không mang thông tin nên model sẽ không học được gì từ cột đó; cần ghi rõ trong README dữ liệu và báo cáo.

### B4. Đề xuất

1. **Bộ 1 – UCI đầy đủ** làm dataset thật chính cho E1 (so chỉ số): thật, cùng nguồn, thêm 16 tháng so với hiện có.
2. **Bộ 2 – Glasgow** (chọn 2–3 cảm biến lưu lượng cao, đủ giờ) làm dataset thật cho E3: COVID là drift thật với mốc rõ ràng (lưu lượng còn ≈ 50%).
3. MnDOT để dự phòng nếu người dùng tải được bằng trình duyệt và có trạm 301.
4. Dữ liệu giả lập hiện tại giữ lại làm dữ liệu kiểm thử tích hợp, không dùng để kết luận độ chính xác.
5. **Tên vùng trên hệ thống:** nếu đưa dữ liệu thật lên hệ thống thì tên vùng nên đúng nơi đo (ví dụ "Minneapolis – I‑94", "Glasgow"), không gắn dữ liệu nước ngoài cho Hà Nội/Hồ Chí Minh. Việc tải dataset lên hệ thống do người dùng quyết định và thực hiện.

### B6. Dataset thật đã dựng (26/11)

Script `data/real/build_real_datasets.py` (`fetch` → `build` → `check`) dựng lại ra đúng từng byte. Nguồn gốc lưu trong `data/real/*/source/`. Mô tả đầy đủ cách điền từng cột ở `data/README.md`.

**Bảng 5. Bốn dataset thật**

| Tệp | Số dòng | Giờ có số đo | Lưu lượng TB (min–max) | Ngày lễ | `air_pollution_index` | Đổi khi COVID (4/2020 ÷ 1–2/2020) | Kiểm tra của website |
| --- | --- | --- | --- | --- | --- | --- | --- |
| I‑94 Minneapolis 2012–2018 | 40.575 | 77,2% | 3.291 (0–7.280) | 53 ngày (Mỹ) | 0 = không có dữ liệu | Không có | Hợp lệ |
| Glasgow `GD1351_R` 2019–2023 | 33.643 | 95,9% | 224 (0–733) | 39 ngày (Scotland) | CAMS EAQI 8–64 | **0,60** | Hợp lệ |
| Glasgow `GD1501_A` 2019–2023 | 33.643 | 95,9% | 186 (0–574) | 39 ngày | CAMS EAQI | **0,54** | Hợp lệ |
| Glasgow `GD0801_A` 2019–2023 | 33.644 | 96,0% | 155 (0–467) | 39 ngày | CAMS EAQI | **0,70** | Hợp lệ |

**Các phát hiện khi dựng (định lượng):**

| Kiểm tra | Kết quả | Hệ quả |
| --- | --- | --- |
| Sáu cột HackerEarth trong dataset gốc | `dew_point` = `visibility_in_miles` ở **100%** số dòng (giá trị 1–9); tương quan độ ẩm–nhiệt độ 0,04; tương quan chỉ số ô nhiễm–lưu lượng −0,004 | Gần như chắc chắn không phải số đo thật → dataset thật lấy các cột này từ ERA5/CAMS hoặc ghi "không có dữ liệu". |
| Dòng trùng giờ trong UCI | 5.445 nhóm trùng; số xe giống nhau 100%, 98% chỉ khác nhãn thời tiết (ví dụ Rain và Drizzle cùng giờ) | Bỏ 7.629 dòng trùng, giữ dòng đầu. |
| Lỗi đo trong UCI | 10 giờ nhiệt độ 0 K; 1 giờ mưa 9.831,3 mm | Thay bằng giá trị ERA5 cùng giờ. |
| Múi giờ UCI (so nhiệt độ UCI với ERA5) | Mùa hè: sai lệch TB 1,34 K ở UTC−5 so với 1,64 K ở UTC−6; mùa đông: 2,17 K ở cả hai | Giờ địa phương có đổi giờ mùa hè (America/Chicago). |
| Múi giờ Glasgow | Giờ cao điểm chiều mùa hè sớm hơn mùa đông 1 giờ (17→16, 16→15) | Giờ UTC. |
| Độ khớp thời tiết | Nhiệt độ ERA5 và nhiệt độ đo của UCI: tương quan 0,98, sai lệch TB 1,69 K | ERA5 đủ tin cậy để bổ sung các cột thiếu. |

**Lưu ý khi đưa lên website:** Model Quality và kiểm tra drift lấy mốc là thời điểm hiện tại, nên dữ liệu lịch sử sẽ không có điểm nào để so. Ngoài ra Model Quality báo lỗi nếu khoảng xem chứa giờ lặp do đổi giờ (vấn đề với I‑94 vì có đổi giờ mùa hè; Glasgow theo UTC thì không). Vì vậy các bộ này dùng cho **thực nghiệm offline** (giai đoạn 3 và 5). Có đưa lên hệ thống hay không là quyết định riêng.

### B5. Nguồn tham khảo (đã mở đọc 25–26/11/2026)

| Tài liệu và liên kết | Tác giả/tổ chức | Dùng cho |
| --- | --- | --- |
| [Metro Interstate Traffic Volume](https://archive.ics.uci.edu/dataset/492/metro+interstate+traffic+volume), DOI 10.24432/C5X60B | J. Hogue, UCI Machine Learning Repository | Xác định nguồn gốc dataset của dự án (B1); ứng viên 1; giấy phép CC BY 4.0. |
| [Predict air pollution index](https://www.hackerearth.com/practice/machine-learning/machine-learning-algorithms/beginners-guide-regression-analysis-plot-interpretations/practice-problems/machine-learning/predict-air-pollution-index-dbb01eb6/) | HackerEarth | Nguồn của sáu cột thêm; trang không nêu nguồn gốc dữ liệu hay giấy phép (B1). |
| [High-resolution traffic flow data in Glasgow](https://zenodo.org/records/14537113) (v2, DOI 10.5281/zenodo.14537113; [v1](https://zenodo.org/records/12100278)) | L. Yue, Q. Zhao, M. Wang (Urban Big Data Centre, University of Glasgow) | Ứng viên 2: lưu lượng theo giờ 10/2019–09/2023, OGL v3.0. |
| [ATR / WIM Hourly Volume Data](https://dot.state.mn.us/traffic/data/reports-hrvol-atr.html) · [Disclaimer](https://www.dot.state.mn.us/information/disclaimer.html) | Minnesota Department of Transportation | Ứng viên 3: Excel theo năm 2017–2024, thông báo dữ liệu 2025 tạm ngưng, điều khoản "as is". |
| [Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api) · [Air Quality API](https://open-meteo.com/en/docs/air-quality-api) · [Terms](https://open-meteo.com/en/terms) | Open-Meteo (P. Zippenfenig); dữ liệu ERA5 của ECMWF/Copernicus, CAMS | Điền thời tiết thật (B3): biến có sẵn, không có visibility, CAMS châu Âu từ 2013, CC BY 4.0, giới hạn 10.000 lượt gọi/ngày cho phi thương mại. |
| [UK bank holidays](https://www.gov.uk/bank-holidays) | GOV.UK (Government Digital Service) | Ngày nghỉ chính thức của Scotland cho Glasgow; chân trang ghi Open Government Licence v3.0 (đọc 26/11). |
