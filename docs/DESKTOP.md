# SHAMS Desktop Client

Ứng dụng desktop (Tauri 2 + React + TypeScript + Tailwind) cho SHAMS Core Service. Thiết kế: ADR-122.

## Chạy

| Cách | Lệnh | Cần |
|---|---|---|
| Cửa sổ desktop (dev) | `scripts\desktop-dev.ps1` | Python venv có SHAMS, Node 20+, Rust (rustup, MSVC), WebView2 |
| Bản trình duyệt (dev, không cần Rust) | `scripts\desktop-dev.ps1 -BrowserOnly` | Python venv, Node 20+ |
| Build installer Windows | `scripts\desktop-build.ps1` → `apps\desktop\src-tauri\target\release\bundle\nsis\*.exe`, `msi\*.msi` | như trên |
| Chỉ backend | `shams desktop-backend C:\AI\SHAMS_Projects --port 8765` | Python venv |

Installer không chứa model hay Python. App dùng Python và model đã có trên máy.

## Cấu hình shell

Lần chạy đầu app ghi `%APPDATA%\com.shams.desktop\config.json`:

```json
{ "shams_dir": "C:\\AI\\SHAMS", "python": "C:\\AI\\SHAMS\\.venv\\Scripts\\python.exe",
  "workspace": "C:\\AI\\SHAMS_Projects", "port": 8765, "attach_existing": true, "start_timeout_s": 90 }
```

Biến môi trường `SHAMS_DIR`, `SHAMS_PYTHON`, `SHAMS_WORKSPACE`, `SHAMS_PORT` ghi đè file này. Nếu đã có backend
chạy ở `port`, app gắn vào nó; nếu không, app tự chạy backend ở cổng trống với token ngẫu nhiên, kiểm tra
`/api/health` mỗi 10 giây, và khi đóng app thì yêu cầu backend tắt (chỉ kill tiến trình do chính app tạo).
Log backend: `%LOCALAPPDATA%\com.shams.desktop\logs\backend.log`.

## Các màn hình

- **New Chat / Chat History**: chat stream (Markdown, code, đính kèm file/ảnh, suy nghĩ của model). Câu trả lời
  hiển thị text trước, rồi gom nhóm: công cụ đã dùng, ảnh/video/3D, liên kết. Lịch sử lưu SQLite, tìm, ghim, lưu trữ.
- **Direct AI**: chọn một model/preset đã đăng ký, chạy thẳng (không Planner). Preset ảnh/video/3D có ô tham số;
  kết quả là artifact của Direct Mode.
- **Agent** (trong chat): chọn Project + chế độ quyền. Agent đọc/sửa file, chạy lệnh/test; thao tác cần duyệt
  hiện thẻ có diff hoặc lệnh; mỗi lượt có thể hoàn tác.
- **Projects**: thư mục local + Environment (biến môi trường, shell, Python/venv, ghi chú cho agent), cây file, Git.
- **Autonomous Workflow**: DAG, worker đang chạy, model đang nạp, chuyển model, VRAM, QC, artifact, lỗi/phục hồi,
  quyết định của người dùng; checkpoint/resume của Core.
- **Media Gallery**: mọi ảnh/video/3D đã tạo, tải về.
- **Model Manager**: model Ollama, file ComfyUI, danh mục SHAMS so với file thật, preset (đăng ký/bật/tắt/xóa),
  nạp/dỡ Ollama (keep_alive), khởi động/giải phóng ComfyUI. Không tự tải model.
- **Settings**: giao diện sáng/tối, quyền mặc định của agent, GPU tuần tự, thông tin backend, `settings.json`.

## Quyền của agent

| Chế độ | Đọc trong Project | Ghi/sửa file | Lệnh an toàn (test, lint, git status...) | Lệnh khác | Phá hủy / nhạy cảm |
|---|---|---|---|---|---|
| Ask Before Action | tự động | hỏi | hỏi | hỏi | hỏi |
| Allow Safe Workspace Actions | tự động | tự động | tự động | hỏi | hỏi |
| Autonomous With Guardrails | tự động | tự động | tự động | tự động | hỏi |

Đường dẫn ngoài Project luôn bị từ chối. `desktop.allow` / `desktop.deny` (glob lệnh) trong settings bổ sung luật.
