$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.IO.Compression.FileSystem

$RepoRoot = Split-Path -Parent $PSScriptRoot
$OutputDir = Join-Path $RepoRoot "reports"
$BuildDir = Join-Path $OutputDir "_chapter1_docx_build"
$OutputPath = Join-Path $OutputDir "Bao_cao_do_an_Chuong_1_Tong_quan.docx"

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
if (Test-Path $BuildDir) {
    Remove-Item -LiteralPath $BuildDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $BuildDir "_rels") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $BuildDir "docProps") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $BuildDir "word") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $BuildDir "word\_rels") | Out-Null

function Escape-Xml([string]$Text) {
    if ($null -eq $Text) { return "" }
    return [System.Security.SecurityElement]::Escape($Text)
}

function RunXml([string]$Text, [switch]$Bold, [switch]$Italic, [string]$Color = "", [int]$Size = 0) {
    $props = ""
    if ($Bold) { $props += "<w:b/>" }
    if ($Italic) { $props += "<w:i/>" }
    if ($Color) { $props += "<w:color w:val=`"$Color`"/>" }
    if ($Size -gt 0) { $half = $Size * 2; $props += "<w:sz w:val=`"$half`"/>" }
    if ($props) {
        return "<w:r><w:rPr>$props</w:rPr><w:t xml:space=`"preserve`">$(Escape-Xml $Text)</w:t></w:r>"
    }
    return "<w:r><w:t xml:space=`"preserve`">$(Escape-Xml $Text)</w:t></w:r>"
}

function Paragraph([string]$Text, [string]$Style = "BodyText", [string]$Align = "", [int]$Before = 0, [int]$After = 120) {
    $jc = if ($Align) { "<w:jc w:val=`"$Align`"/>" } else { "" }
    $spacing = "<w:spacing w:before=`"$Before`" w:after=`"$After`" w:line=`"360`" w:lineRule=`"auto`"/>"
    return "<w:p><w:pPr><w:pStyle w:val=`"$Style`"/>$spacing$jc</w:pPr>$(RunXml $Text)</w:p>"
}

function Heading([string]$Text, [int]$Level) {
    $style = "Heading$Level"
    $after = if ($Level -eq 1) { 180 } else { 120 }
    return "<w:p><w:pPr><w:pStyle w:val=`"$style`"/><w:spacing w:before=`"220`" w:after=`"$after`"/></w:pPr>$(RunXml $Text)</w:p>"
}

function TitlePara([string]$Text, [int]$Size = 24, [string]$Color = "1F4E79") {
    return "<w:p><w:pPr><w:jc w:val=`"center`"/><w:spacing w:before=`"120`" w:after=`"160`"/></w:pPr>$(RunXml $Text -Bold -Color $Color -Size $Size)</w:p>"
}

function SubtitlePara([string]$Text) {
    return "<w:p><w:pPr><w:jc w:val=`"center`"/><w:spacing w:after=`"120`"/></w:pPr>$(RunXml $Text -Italic -Color "5B677A" -Size 12)</w:p>"
}

function PageBreak() {
    return "<w:p><w:r><w:br w:type=`"page`"/></w:r></w:p>"
}

function Bullet([string]$Text) {
    return "<w:p><w:pPr><w:pStyle w:val=`"BodyText`"/><w:numPr><w:ilvl w:val=`"0`"/><w:numId w:val=`"1`"/></w:numPr><w:spacing w:after=`"80`" w:line=`"360`" w:lineRule=`"auto`"/></w:pPr>$(RunXml $Text)</w:p>"
}

function TableCell([string]$Text, [switch]$Header) {
    $shade = if ($Header) { "<w:shd w:fill=`"EAF2F8`"/>" } else { "" }
    $run = if ($Header) { RunXml $Text -Bold -Color "1F4E79" } else { RunXml $Text }
    return "<w:tc><w:tcPr><w:tcW w:w=`"3100`" w:type=`"dxa`"/>$shade</w:tcPr><w:p><w:pPr><w:spacing w:after=`"60`"/></w:pPr>$run</w:p></w:tc>"
}

function SimpleTable([object[]]$Rows) {
    $xml = "<w:tbl><w:tblPr><w:tblStyle w:val=`"TableGrid`"/><w:tblW w:w=`"0`" w:type=`"auto`"/><w:tblBorders><w:top w:val=`"single`" w:sz=`"4`" w:space=`"0`" w:color=`"D0D7DE`"/><w:left w:val=`"single`" w:sz=`"4`" w:space=`"0`" w:color=`"D0D7DE`"/><w:bottom w:val=`"single`" w:sz=`"4`" w:space=`"0`" w:color=`"D0D7DE`"/><w:right w:val=`"single`" w:sz=`"4`" w:space=`"0`" w:color=`"D0D7DE`"/><w:insideH w:val=`"single`" w:sz=`"4`" w:space=`"0`" w:color=`"D0D7DE`"/><w:insideV w:val=`"single`" w:sz=`"4`" w:space=`"0`" w:color=`"D0D7DE`"/></w:tblBorders></w:tblPr>"
    for ($i = 0; $i -lt $Rows.Count; $i++) {
        $row = $Rows[$i]
        $xml += "<w:tr>"
        foreach ($cell in $row) {
            if ($i -eq 0) { $xml += TableCell $cell -Header } else { $xml += TableCell $cell }
        }
        $xml += "</w:tr>"
    }
    $xml += "</w:tbl>"
    return $xml
}

$body = New-Object System.Collections.Generic.List[string]
$body.Add((TitlePara "BÁO CÁO ĐỒ ÁN CHUYÊN NGÀNH" 22))
$body.Add((TitlePara "CHƯƠNG I. TỔNG QUAN" 26))
$body.Add((SubtitlePara "Đề tài: Xây dựng quy trình MLOps thích ứng với Data Drift cho dự báo lưu lượng giao thông"))
$body.Add((SubtitlePara "Building a Data Drift-Adaptive MLOps Pipeline for Traffic Flow Forecasting"))
$body.Add((Paragraph "Chương này trình bày bối cảnh hình thành đề tài, vấn đề cần giải quyết, mục tiêu nghiên cứu, phạm vi thực hiện, phương pháp tiếp cận và ý nghĩa của hệ thống. Nội dung được viết dựa trên kiến trúc hiện tại của dự án: ứng dụng web quản trị, API FastAPI, cụm k3s, Airflow, MLflow, MinIO, PostgreSQL, quy trình huấn luyện nhiều mô hình dự báo và cơ chế giám sát feature drift để kích hoạt retrain." "BodyText" "both" 220 120))
$body.Add((PageBreak))

$body.Add((Heading "1.1. Bối cảnh và động lực thực hiện đề tài" 1))
$body.Add((Paragraph "Dự báo lưu lượng giao thông là một bài toán quan trọng trong quản lý đô thị thông minh. Khi có khả năng ước lượng trước lưu lượng phương tiện theo giờ hoặc theo ngày, hệ thống điều hành giao thông có thể hỗ trợ ra quyết định tốt hơn trong nhiều tình huống: nhận diện khung giờ cao điểm, chuẩn bị phương án phân luồng, đánh giá tác động của thời tiết và ngày lễ, cũng như cung cấp thông tin dự báo cho người dùng cuối. Trong bối cảnh dữ liệu giao thông ngày càng được thu thập liên tục từ nhiều nguồn khác nhau, bài toán dự báo không chỉ dừng lại ở việc xây dựng một mô hình học máy có độ chính xác tốt tại thời điểm huấn luyện ban đầu." "BodyText" "both"))
$body.Add((Paragraph "Một đặc điểm nổi bật của dữ liệu giao thông là tính biến động theo thời gian. Lưu lượng phương tiện có thể thay đổi do mùa vụ, lịch làm việc, sự kiện đô thị, điều kiện thời tiết, quy hoạch hạ tầng, thay đổi hành vi di chuyển hoặc thay đổi trong chính nguồn dữ liệu thu thập. Vì vậy, mô hình được huấn luyện tốt trên một giai đoạn lịch sử có thể suy giảm hiệu quả khi phân phối dữ liệu thực tế thay đổi. Hiện tượng này thường được gọi là data drift hoặc feature drift, tức là dữ liệu đầu vào trong môi trường vận hành không còn giống với dữ liệu mà mô hình đã học." "BodyText" "both"))
$body.Add((Paragraph "Trong các hệ thống học máy truyền thống, quá trình huấn luyện, đánh giá, triển khai và giám sát mô hình thường được thực hiện rời rạc. Mỗi khi dữ liệu thay đổi hoặc mô hình cần cập nhật, nhóm phát triển phải thao tác thủ công nhiều bước: chuẩn bị dữ liệu, chạy lại mã huấn luyện, so sánh kết quả, thay mô hình đang dùng và kiểm tra dịch vụ dự báo. Cách làm này khó mở rộng khi hệ thống có nhiều khu vực, nhiều phiên bản mô hình, nhiều người quản trị và yêu cầu vận hành liên tục. MLOps ra đời nhằm giải quyết vấn đề này bằng cách đưa các thực hành DevOps vào vòng đời học máy: tự động hóa pipeline, quản lý dữ liệu và artifact, theo dõi thí nghiệm, kiểm soát phiên bản mô hình, triển khai có kiểm soát và giám sát sau triển khai." "BodyText" "both"))
$body.Add((Paragraph "Đề tài này được xây dựng từ nhu cầu kết hợp hai hướng: dự báo lưu lượng giao thông và quy trình MLOps thích ứng với drift. Thay vì chỉ huấn luyện một mô hình dự báo, hệ thống hướng đến một pipeline hoàn chỉnh cho phép quản trị viên tạo khu vực, tải dữ liệu CSV, cấu hình huấn luyện, chạy nhiều mô hình ứng viên song song, chọn mô hình tốt nhất theo tiêu chí công bằng, đưa mô hình lên production theo từng khu vực, phục vụ dashboard/dự báo và theo dõi drift để đề xuất hoặc tự động kích hoạt huấn luyện lại." "BodyText" "both"))

$body.Add((Heading "1.2. Vấn đề đặt ra" 1))
$body.Add((Paragraph "Bài toán trung tâm của đề tài là xây dựng một hệ thống MLOps có khả năng vận hành mô hình dự báo lưu lượng giao thông trong môi trường dữ liệu thay đổi theo thời gian. Hệ thống cần giải quyết đồng thời nhiều lớp vấn đề: lớp dữ liệu, lớp mô hình, lớp triển khai, lớp giám sát và lớp quản trị." "BodyText" "both"))
$body.Add((Paragraph "Ở lớp dữ liệu, hệ thống phải tiếp nhận dữ liệu giao thông theo khu vực, kiểm tra tính hợp lệ của schema, khoảng thời gian và chất lượng dữ liệu trước khi đưa vào huấn luyện. Dữ liệu cần được lưu trữ ổn định để pipeline Airflow có thể truy xuất lại, đồng thời metadata về dataset như số dòng, khoảng thời gian và trạng thái hợp lệ phải được ghi nhận trong cơ sở dữ liệu." "BodyText" "both"))
$body.Add((Paragraph "Ở lớp mô hình, hệ thống không chỉ huấn luyện một thuật toán duy nhất mà cần so sánh nhiều ứng viên gồm các mô hình cây quyết định tăng cường và mô hình học sâu tuần tự. Các mô hình phải được đánh giá trên cùng một cửa sổ huấn luyện, cùng chính sách chia tập theo thời gian, cùng số fold cross validation và cùng final test ratio để việc chọn best model có tính công bằng. Với dữ liệu chuỗi thời gian, không thể chia ngẫu nhiên như bài toán hồi quy thông thường vì điều đó gây rò rỉ thông tin tương lai; do đó hệ thống phải dùng phương pháp expanding-window cross validation." "BodyText" "both"))
$body.Add((Paragraph "Ở lớp triển khai, mỗi region cần có mô hình production riêng. Khi quản trị viên chọn region ở giao diện, toàn bộ dashboard, prediction, dataset, model và drift phải làm việc theo region đó. Vì vậy hệ thống phải đảm bảo trạng thái active model được gắn đúng region, tránh việc huấn luyện hoặc kích hoạt mô hình cho một khu vực nhưng lại ảnh hưởng sang khu vực khác." "BodyText" "both"))
$body.Add((Paragraph "Ở lớp giám sát, mô hình cần được theo dõi sau khi đưa vào sử dụng. Nếu dữ liệu production lệch đáng kể so với dữ liệu tham chiếu tại thời điểm huấn luyện, hệ thống phải ghi nhận drift, hiển thị chi tiết feature score cho quản trị viên và có khả năng kích hoạt retrain với training window phù hợp. Yêu cầu quan trọng là cơ chế drift phải bổ sung vào logic hiện có, không làm phá vỡ luồng train, deploy và predict đã ổn định." "BodyText" "both"))
$body.Add((Paragraph "Từ các yêu cầu trên, đề tài đặt ra câu hỏi: làm thế nào thiết kế một hệ thống MLOps đủ đơn giản để triển khai trên hạ tầng hạn chế, nhưng vẫn có đầy đủ các thành phần cốt lõi như quản lý dữ liệu, huấn luyện tự động, tracking thí nghiệm, registry mô hình, serving dự báo, giám sát drift và retrain có kiểm soát?" "BodyText" "both"))

$body.Add((Heading "1.3. Mục tiêu của đề tài" 1))
$body.Add((Paragraph "Mục tiêu tổng quát của đề tài là xây dựng một quy trình MLOps thích ứng với data drift cho bài toán dự báo lưu lượng giao thông, trong đó mô hình có thể được huấn luyện, đánh giá, triển khai, giám sát và cập nhật lại theo từng region thông qua giao diện web và pipeline tự động." "BodyText" "both"))
$body.Add((Paragraph "Các mục tiêu cụ thể gồm:" "BodyText" "both"))
$body.Add((Bullet "Xây dựng ứng dụng web quản trị cho phép đăng nhập, quản lý region, tải lên dataset CSV, cấu hình huấn luyện, chọn danh sách mô hình cần train, xem model versions, kích hoạt model production, dự báo lưu lượng và theo dõi drift."))
$body.Add((Bullet "Xây dựng backend API bằng FastAPI để cung cấp các endpoint cho frontend, quản lý dữ liệu trong PostgreSQL, lưu dataset/artifact trong MinIO và kết nối với Airflow, MLflow."))
$body.Add((Bullet "Hiện thực pipeline huấn luyện theo region, trong đó Airflow điều phối các nhánh song song gồm nhóm model cây, LSTM và GRU; các nhánh chỉ chạy khi được chọn trong cấu hình huấn luyện."))
$body.Add((Bullet "Đánh giá các mô hình bằng expanding-window cross validation, chọn best model theo CV mean MAE, đồng thời ghi nhận final test MAE để báo cáo chất lượng mô hình trên tập kiểm tra độc lập."))
$body.Add((Bullet "Quản lý vòng đời mô hình theo region: lưu model version, variant, metric, artifact path, MLflow run, trạng thái candidate/active/archived và cho phép kích hoạt mô hình production."))
$body.Add((Bullet "Cung cấp dashboard dự báo 24 giờ tiếp theo, dự báo 7 ngày và API dự đoán theo thời điểm dựa trên mô hình active của region đang chọn."))
$body.Add((Bullet "Bổ sung feature drift monitoring: so sánh cửa sổ reference với cửa sổ current trong production data, tính điểm drift theo từng feature, lưu lịch sử drift và hỗ trợ trigger retrain khi cần."))
$body.Add((Bullet "Triển khai hệ thống lên cụm k3s gồm các dịch vụ API, Airflow, MLflow, MinIO và PostgreSQL, đồng thời kết nối frontend được phát hành qua GitHub Pages với API public domain."))

$body.Add((Heading "1.4. Đối tượng và phạm vi nghiên cứu" 1))
$body.Add((Paragraph "Đối tượng nghiên cứu của đề tài là dữ liệu lưu lượng giao thông theo thời gian, trong đó biến mục tiêu là traffic_volume và các biến đầu vào bao gồm thông tin thời gian, thời tiết, ngày lễ và các đặc trưng chuỗi thời gian được tạo trong pipeline. Dữ liệu được tổ chức theo từng region, ví dụ Chicago hoặc Thành phố Hà Nội, để hệ thống có thể huấn luyện và phục vụ mô hình riêng cho từng khu vực." "BodyText" "both"))
$body.Add((Paragraph "Phạm vi bài toán tập trung vào dự báo lưu lượng giao thông dạng hồi quy theo giờ. Hệ thống hiện thực các mô hình cây như Random Forest, XGBoost, LightGBM và các mô hình học sâu tuần tự LSTM, GRU. Trong quá trình huấn luyện, các mô hình được so sánh trên cùng một cửa sổ thời gian và cùng chính sách đánh giá để giảm thiên lệch khi chọn model production." "BodyText" "both"))
$body.Add((Paragraph "Về mặt MLOps, đề tài tập trung vào các thành phần thiết yếu: quản lý dataset, orchestration bằng Airflow, tracking bằng MLflow, lưu trữ artifact bằng MinIO/PVC, quản lý metadata bằng PostgreSQL, serving dự báo bằng FastAPI, giao diện quản trị bằng React và giám sát feature drift. Đề tài chưa hướng đến các bài toán lớn hơn như stream processing thời gian thực ở quy mô rất lớn, huấn luyện phân tán trên GPU cluster, tối ưu chi phí cloud toàn diện hoặc triển khai blue-green/canary phức tạp." "BodyText" "both"))
$body.Add((Paragraph "Về drift, hệ thống tập trung vào feature/data drift, tức là sự thay đổi phân phối của các đặc trưng đầu vào giữa dữ liệu tham chiếu lúc huấn luyện và dữ liệu production sau khi mô hình hoạt động. Performance drift dựa trên ground truth thực tế có thể là hướng mở rộng trong tương lai, vì trong nhiều hệ thống dự báo giao thông, nhãn thực tế của thời điểm tương lai không phải lúc nào cũng có ngay tại thời điểm dự đoán." "BodyText" "both"))

$body.Add((Heading "1.5. Phương pháp tiếp cận" 1))
$body.Add((Paragraph "Hệ thống được thiết kế theo hướng pipeline hoá toàn bộ vòng đời mô hình. Khi quản trị viên tải dataset lên, backend kiểm tra schema và chất lượng dữ liệu, sau đó lưu file vào MinIO và lưu metadata vào PostgreSQL. Khi bấm Train, frontend mở modal để chọn các mô hình cần huấn luyện; backend tạo training run và kích hoạt DAG Airflow tương ứng. Airflow giữ vai trò điều phối, còn logic huấn luyện thực thi trong API thông qua các endpoint nội bộ. Cách tách vai trò này giúp Airflow quản lý trạng thái workflow, còn API giữ quyền truy cập dữ liệu, artifact và database." "BodyText" "both"))
$body.Add((Paragraph "Trong quá trình huấn luyện, dữ liệu được chuyển thành dạng hourly time series, tạo thêm đặc trưng thời gian và các đặc trưng phục vụ mô hình. Cửa sổ huấn luyện được chia theo thứ tự thời gian thành development và final test. Development tiếp tục được chia thành nhiều expanding-window fold để tính validation metric; final test chỉ dùng để đánh giá sau cùng, không dùng để chọn mô hình. Best model được chọn theo cross_validation_mean_MAE nhằm bảo đảm rằng quyết định chọn model dựa trên năng lực tổng quát qua nhiều fold thay vì một lát cắt dữ liệu duy nhất." "BodyText" "both"))
$body.Add((Paragraph "Sau huấn luyện, hệ thống ghi nhận kết quả vào model registry nội bộ và MLflow. Model registry trong PostgreSQL lưu các thông tin phục vụ nghiệp vụ như version, variant, trạng thái active/candidate/archived, metric và liên kết đến artifact. MLflow lưu thí nghiệm, metric theo fold, metric tổng hợp và artifact phục vụ truy vết. MinIO/PVC lưu dataset và model artifact để có thể tái sử dụng trong dự báo hoặc kiểm tra lại." "BodyText" "both"))
$body.Add((Paragraph "Đối với drift monitoring, hệ thống dùng hai cửa sổ dữ liệu. Reference window là 30 ngày cuối trước train_end_date của mô hình active, đại diện cho phân phối dữ liệu gần nhất mà mô hình đã học. Current window là 7 ngày trong production data, mặc định kết thúc tại thời điểm hệ thống hoặc theo mốc do quản trị viên chọn để kiểm thử. Với feature số, hệ thống tính Population Stability Index (PSI); với feature phân loại, hệ thống tính Jensen-Shannon divergence. Nếu số feature vượt ngưỡng đạt tối thiểu theo cấu hình, hệ thống ghi nhận drift_detected và có thể trigger retrain theo cấu hình auto retrain." "BodyText" "both"))
$body.Add((Paragraph "Về triển khai, hệ thống chạy trên cụm k3s hai node. Frontend được phát hành qua GitHub Pages và gọi API thông qua domain public. Lưu lượng đi qua Nginx/ingress trên EC2 node public rồi đến service traffic-api trong cluster. Các thành phần nền tảng như PostgreSQL, MinIO, MLflow, Airflow scheduler/webserver và traffic-api được triển khai bằng manifest Kubernetes, giúp hệ thống có thể restart, rollout và kiểm tra health tương đối nhất quán." "BodyText" "both"))

$body.Add((Heading "1.6. Kiến trúc tổng quan của hệ thống" 1))
$body.Add((Paragraph "Ở mức tổng quan, hệ thống có thể được chia thành ba nhóm chức năng chính: Training, Monitoring và Deployment/Serving." "BodyText" "both"))
$body.Add((SimpleTable @(
    @("Nhóm", "Thành phần chính", "Vai trò trong hệ thống"),
    @("Training", "Frontend Datasets/Models, FastAPI, Airflow training DAG, PostgreSQL, MinIO, MLflow, mã huấn luyện trong src/", "Tiếp nhận dataset, tạo training run, huấn luyện các model ứng viên, đánh giá bằng cross validation, lưu artifact và chọn best model."),
    @("Monitoring", "Frontend Drift, FastAPI drift API, Airflow drift DAG, PostgreSQL DriftCheck, production data từ prediction service", "So sánh reference window và current window, tính feature score, lưu lịch sử drift, hỗ trợ manual/auto retrain."),
    @("Deployment/Serving", "GitHub Pages frontend, Nginx/Ingress, traffic-api, active model per region, dashboard và prediction endpoints", "Phục vụ giao diện người dùng, dự báo lưu lượng theo region và quản lý mô hình production.")
)))
$body.Add((Paragraph "Frontend đóng vai trò là giao diện thao tác chính của người dùng. Admin có thể chọn region ở góc phải; lựa chọn này chi phối dữ liệu hiển thị ở các tab Dashboard, Prediction, Regions, Datasets, Models và Drift. Backend FastAPI là lớp nghiệp vụ trung tâm: nhận yêu cầu từ frontend, kiểm soát quyền, truy vấn database, gọi Airflow, đọc/ghi MinIO, làm việc với MLflow và thực hiện dự báo bằng mô hình active. Airflow không trực tiếp xử lý toàn bộ logic huấn luyện mà đóng vai trò điều phối workflow; các task trong DAG gọi API nội bộ để thực thi từng nhánh train và finalize." "BodyText" "both" 160 120))
$body.Add((Paragraph "PostgreSQL giữ metadata quan trọng như user, region, dataset, training run, model version, prediction và drift check. MinIO lưu dataset upload và artifact dạng object storage. MLflow lưu thông tin thí nghiệm, metric, artifact và giúp theo dõi quá trình huấn luyện theo từng region. Nhờ sự phân tách này, hệ thống có thể vừa phục vụ nghiệp vụ web, vừa đảm bảo khả năng truy vết vòng đời mô hình." "BodyText" "both"))

$body.Add((Heading "1.7. Ý nghĩa khoa học và thực tiễn" 1))
$body.Add((Paragraph "Về mặt khoa học, đề tài minh họa cách kết hợp phương pháp dự báo chuỗi thời gian với quy trình MLOps thích ứng drift. Hệ thống không đánh giá mô hình bằng một lần train-test đơn giản, mà sử dụng expanding-window cross validation phù hợp với dữ liệu thời gian. Cách chọn best model theo CV mean MAE giúp quyết định chọn mô hình có cơ sở hơn, đồng thời final test metric được tách riêng để phản ánh chất lượng trên tập kiểm tra độc lập." "BodyText" "both"))
$body.Add((Paragraph "Về mặt kỹ thuật, đề tài thể hiện một kiến trúc MLOps tương đối đầy đủ trong điều kiện triển khai thực tế: có web quản trị, API, cơ sở dữ liệu, object storage, MLflow tracking, Airflow orchestration, Kubernetes deployment, health check và drift monitoring. Hệ thống cho thấy cách tổ chức các thành phần này để hỗ trợ một bài toán học máy cụ thể thay vì chỉ dừng ở các script huấn luyện rời rạc." "BodyText" "both"))
$body.Add((Paragraph "Về mặt thực tiễn, hệ thống hỗ trợ quản trị nhiều region, nhiều dataset và nhiều phiên bản mô hình. Khi dữ liệu production thay đổi, quản trị viên có thể quan sát drift, xem feature score, xem lịch sử kiểm tra và kích hoạt retrain. Điều này giúp giảm rủi ro sử dụng mô hình đã lỗi thời, đồng thời tạo nền tảng cho các hệ thống giao thông thông minh cần cập nhật mô hình định kỳ." "BodyText" "both"))

$body.Add((Heading "1.8. Cấu trúc báo cáo" 1))
$body.Add((Paragraph "Báo cáo đồ án được tổ chức thành sáu chương. Chương I trình bày tổng quan về bối cảnh, vấn đề, mục tiêu, phạm vi và phương pháp tiếp cận của đề tài. Chương II trình bày cơ sở lý thuyết liên quan đến dự báo chuỗi thời gian, các mô hình học máy/học sâu, MLOps, Airflow, MLflow, Kubernetes và data drift. Chương III phân tích yêu cầu và thiết kế hệ thống, bao gồm kiến trúc tổng thể, mô hình dữ liệu, luồng nghiệp vụ và thiết kế pipeline." "BodyText" "both"))
$body.Add((Paragraph "Chương IV mô tả quá trình hiện thực hệ thống, bao gồm backend FastAPI, frontend React, pipeline huấn luyện, model registry, prediction service, drift monitoring và triển khai trên k3s. Chương V trình bày thực nghiệm, đánh giá và bàn luận: dữ liệu sử dụng, cấu hình huấn luyện, kết quả các mô hình, thời gian chạy, hạn chế tài nguyên và đánh giá hiệu quả của cơ chế drift/retrain. Chương VI tổng kết kết quả đạt được, các hạn chế còn tồn tại và hướng phát triển trong tương lai." "BodyText" "both"))

$body.Add((Heading "1.9. Kết chương" 1))
$body.Add((Paragraph "Chương I đã giới thiệu tổng quan đề tài Xây dựng quy trình MLOps thích ứng với Data Drift cho dự báo lưu lượng giao thông. Đề tài xuất phát từ nhu cầu vận hành mô hình dự báo trong môi trường dữ liệu thay đổi, nơi việc huấn luyện một lần là chưa đủ. Hệ thống hướng đến một pipeline hoàn chỉnh gồm quản lý dữ liệu, huấn luyện nhiều mô hình, chọn best model công bằng, triển khai mô hình theo region, phục vụ dự báo, giám sát feature drift và hỗ trợ retrain. Các chương tiếp theo sẽ trình bày nền tảng lý thuyết, thiết kế chi tiết, quá trình hiện thực và kết quả thực nghiệm của hệ thống." "BodyText" "both"))

$documentBody = ($body -join "`n")

$contentTypes = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
  <Override PartName="/word/fontTable.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
'@

$rels = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
'@

$docRels = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
'@

$styles = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/><w:color w:val="111827"/></w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
  <w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:jc w:val="both"/></w:pPr><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="BodyText"/><w:qFormat/><w:pPr><w:keepNext/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:b/><w:color w:val="1F4E79"/><w:sz w:val="32"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="BodyText"/><w:qFormat/><w:pPr><w:keepNext/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:b/><w:color w:val="1F4E79"/><w:sz w:val="26"/></w:rPr></w:style>
  <w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/><w:uiPriority w:val="39"/><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:color="D0D7DE"/><w:left w:val="single" w:sz="4" w:color="D0D7DE"/><w:bottom w:val="single" w:sz="4" w:color="D0D7DE"/><w:right w:val="single" w:sz="4" w:color="D0D7DE"/><w:insideH w:val="single" w:sz="4" w:color="D0D7DE"/><w:insideV w:val="single" w:sz="4" w:color="D0D7DE"/></w:tblBorders></w:tblPr></w:style>
</w:styles>
'@

$numbering = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0">
    <w:multiLevelType w:val="hybridMultilevel"/>
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="bullet"/>
      <w:lvlText w:val="•"/>
      <w:lvlJc w:val="left"/>
      <w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>
      <w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/></w:rPr>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>
'@

$settings = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:zoom w:percent="100"/>
  <w:defaultTabStop w:val="720"/>
  <w:compat/>
</w:settings>
'@

$fontTable = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:font w:name="Calibri"><w:family w:val="swiss"/></w:font>
  <w:font w:name="Symbol"><w:family w:val="roman"/></w:font>
</w:fonts>
'@

$nowIso = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$core = @"
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Báo cáo đồ án - Chương I. Tổng quan</dc:title>
  <dc:subject>Xây dựng quy trình MLOps thích ứng với Data Drift cho dự báo lưu lượng giao thông</dc:subject>
  <dc:creator>Codex</dc:creator>
  <cp:lastModifiedBy>Codex</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">$nowIso</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">$nowIso</dcterms:modified>
</cp:coreProperties>
"@

$app = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Codex OOXML Generator</Application>
  <DocSecurity>0</DocSecurity>
  <ScaleCrop>false</ScaleCrop>
  <Company>UIT NT114 Project</Company>
</Properties>
'@

$document = @"
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:w10="urn:schemas-microsoft-com:office:word" xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk" xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml" xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" mc:Ignorable="w14 wp14">
  <w:body>
    $documentBody
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>
      <w:cols w:space="720"/>
      <w:docGrid w:linePitch="360"/>
    </w:sectPr>
  </w:body>
</w:document>
"@

Set-Content -LiteralPath (Join-Path $BuildDir "[Content_Types].xml") -Value $contentTypes -Encoding UTF8
Set-Content -LiteralPath (Join-Path $BuildDir "_rels\.rels") -Value $rels -Encoding UTF8
Set-Content -LiteralPath (Join-Path $BuildDir "word\_rels\document.xml.rels") -Value $docRels -Encoding UTF8
Set-Content -LiteralPath (Join-Path $BuildDir "word\styles.xml") -Value $styles -Encoding UTF8
Set-Content -LiteralPath (Join-Path $BuildDir "word\numbering.xml") -Value $numbering -Encoding UTF8
Set-Content -LiteralPath (Join-Path $BuildDir "word\settings.xml") -Value $settings -Encoding UTF8
Set-Content -LiteralPath (Join-Path $BuildDir "word\fontTable.xml") -Value $fontTable -Encoding UTF8
Set-Content -LiteralPath (Join-Path $BuildDir "word\document.xml") -Value $document -Encoding UTF8
Set-Content -LiteralPath (Join-Path $BuildDir "docProps\core.xml") -Value $core -Encoding UTF8
Set-Content -LiteralPath (Join-Path $BuildDir "docProps\app.xml") -Value $app -Encoding UTF8

if (Test-Path $OutputPath) {
    Remove-Item -LiteralPath $OutputPath -Force
}
[System.IO.Compression.ZipFile]::CreateFromDirectory($BuildDir, $OutputPath)
Write-Output $OutputPath
