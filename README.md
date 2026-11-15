# FuOverflow: tải ảnh đề và ghép PDF

Bộ công cụ tải attachment ảnh được đánh số `Q1`, `Q2`, ... từ một bài viết FuOverflow, lưu ảnh gốc và ghép mỗi ảnh thành một trang PDF. Bộ này đã được thử với đề MAS291 SU26 RE; các bài có tên ảnh hoặc cấu trúc khác có thể không được nhận diện đầy đủ. Hãy luôn đối chiếu số ảnh với danh sách attachment trên website.

## Yêu cầu

- Fedora Linux x86_64 (các Linux x86_64 dùng glibc 2.35 trở lên có thể tương thích), Python 3, `python3-pip`, Node.js 22 trở lên, curl và Internet.
- Một tài khoản FuOverflow có quyền xem ảnh gốc nếu bài viết yêu cầu đăng nhập.
- Lightpanda là trình duyệt headless; bộ cài tải binary nightly chính thức, không cài Chromium.

## 1. Cài đặt

Giải nén gói và mở terminal tại thư mục `fuoverflow_downloader`:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x install_lightpanda.sh
./install_lightpanda.sh
tools/lightpanda/lightpanda version
node --version
```

Tệp cài đặt và binary được tải về từ [repository chính thức Lightpanda](https://github.com/lightpanda-io/browser). Phiên bản nightly có thể thay đổi. Công cụ tắt telemetry của Lightpanda khi chạy.

## 2. Đăng nhập và tạo session

Đăng nhập FuOverflow trong trình duyệt của bạn và hoàn thành CAPTCHA thủ công nếu website yêu cầu. Từ một tiện ích xuất cookie mà bạn tin tưởng, xuất **chỉ cookie của fuoverflow.com** ở định dạng JSON (danh sách cookie như Cookie-Editor). Không xuất toàn bộ profile hay mật khẩu.

Trong thư mục công cụ, tạo nơi lưu riêng:

```bash
mkdir -p session
chmod 700 session
```

Lưu JSON vào `session/fuoverflow.cookies.json`, rồi chạy:

```bash
chmod 600 session/fuoverflow.cookies.json
```

Cookie cấp quyền truy cập tài khoản: không chia sẻ, không commit, không dán vào chat. Nếu dùng thiết bị dùng chung, giữ file ở nơi an toàn hoặc xóa sau khi dùng. Nếu session hết hạn, đăng nhập và xuất lại. Thư mục `session/` đã được thêm vào `.gitignore`; `.gitignore` không bảo vệ file khi tự tạo ZIP hoặc chia sẻ thủ công.

## 3. Tải một đề

Sao chép URL bài viết, đặt tên thư mục đầu ra theo học kỳ và loại đề, rồi chạy ví dụ:

```bash
.venv/bin/python downloader.py \
  --url 'https://fuoverflow.com/threads/mas291-su26-re.7211/' \
  --output output/MAS291_SU26_RE \
  --cookies session/fuoverflow.cookies.json
```

Đổi cả `--url` và `--output` cho mỗi đề. SP, SU và FA là các học kỳ khác nhau; FE và RE là loại kỳ thi khác nhau. Nếu bài cho phép truy cập công khai, có thể bỏ `--cookies`.

Kết quả trong thư mục đầu ra:

```text
MAS291_SU26_RE/
├── original_images/       # ảnh tải về
├── MAS291_SU26_RE.pdf     # mỗi ảnh một trang
└── download_report.txt    # nguồn, độ phân giải, số trang và lỗi
```

Thư mục ẩn `.work/` chứa DOM, URL và log chẩn đoán có thể bao gồm dữ liệu riêng hoặc liên kết có thời hạn; không chia sẻ nguyên thư mục này. `rejected_thumbnails/` (nếu có) chứa các ảnh độ phân giải thấp từ lần chạy cũ, không dùng để đọc đề.

## 4. Kiểm tra

Mở PDF, kiểm tra trang đầu, giữa và cuối; đối chiếu số trang với số attachment trên bài viết và đọc `download_report.txt`. Ví dụ với Poppler:

```bash
pdfinfo output/MAS291_SU26_RE/MAS291_SU26_RE.pdf | grep Pages
file output/MAS291_SU26_RE/original_images/Q2.webp
```

Ảnh gốc giữ nguyên định dạng và byte tải về. PDF mã hóa lossless pixel ảnh, giữ tỷ lệ, không crop hoặc OCR. Với ảnh có EXIF orientation, PDF hiển thị theo orientation đó. Bộ lọc từ chối ảnh cạnh lớn nhất ≤500 px để tránh thumbnail; một ảnh gốc nhỏ thực sự cũng sẽ bị từ chối. Việc so sánh độ phân giải lớn nhất chỉ là phép kiểm tra, không chứng minh tất cả ảnh là original trong mọi cấu trúc website.

## Khắc phục lỗi

- `HTTP 403`, không có ảnh: đăng nhập lại bằng trình duyệt, thử mở ảnh gốc, xuất lại cookie đúng website. Trang có thể áp dụng kiểm soát bổ sung; công cụ không vượt CAPTCHA hay giới hạn truy cập.
- `Lightpanda binary missing`: chạy lại `./install_lightpanda.sh`. Khi Lightpanda lỗi, script thử HTTP trực tiếp; không tự cài Chromium.
- `Detected questions: 0`: trang có thể dùng cách đặt tên khác `Q<number>`, không có attachment hoặc thay đổi cấu trúc. Bộ này không hỗ trợ đầy đủ mọi bài FuOverflow.
- Khi tải thiếu, xem các dòng `Notes` và `Failed/rejected images` trong báo cáo. Script không tạo PDF mới nếu ảnh được nhận diện bị lỗi hoặc không phân giải được; PDF cũ, nếu có, vẫn có thể tồn tại.
- Chỉ số `Detected questions` đếm câu nhận diện được, không chứng minh đã phát hiện mọi attachment. Các file không theo tên Q hoặc nhiều file cùng số câu có thể bị bỏ sót.
- Nếu thấy cổng 9222 đang được dùng, dừng tiến trình Lightpanda khác trước khi chạy. Không mở CDP ra mạng công cộng.
# exam-pdf-toolkit
