# Internet Access trong SHAMS

Worker Ollama có tool calling tự quyết định lúc cần tra cứu. `ToolBox` cấp công cụ Research cho `LLMWorker` và `DirectChatWorker`. Trình điều phối gọi công cụ, trả dữ liệu lại cho model rồi tiếp tục vòng suy luận. Web Chat cấp riêng ba công cụ tìm và đọc công khai khi Research bật và model Ollama khai báo khả năng tool calling. Nguồn thực sự đọc được hiển thị dưới câu trả lời. Coding Agent riêng của Desktop ở PR #7 dùng catalog khác và chưa được nối vào module này.

Thiết kế tham khảo hai plugin [web-browsing](https://github.com/Mintplex-Labs/anything-llm/blob/master/server/utils/agents/aibitat/plugins/web-browsing.js) và [web-scraping](https://github.com/Mintplex-Labs/anything-llm/blob/master/server/utils/agents/aibitat/plugins/web-scraping.js) của AnythingLLM. SHAMS giữ catalog, transport Ollama, vòng gọi công cụ và handoff hiện có.

## Cấu hình

Gộp các trường sau vào mục `research` trong `%USERPROFILE%\.shams\settings.json`. `enabled` mặc định tắt. Có thể tắt nghiên cứu cho một lượt Direct bằng `--param research=false`.

```json
{
  "research": {
    "enabled": true,
    "search_url": "http://127.0.0.1:8888/search?q={query}&format=json",
    "timeout": 12,
    "max_seconds": 45,
    "max_calls": 6,
    "max_searches": 3,
    "context_bytes": 14000
  }
}
```

`search_url` là endpoint do người vận hành cấu hình. Nếu bỏ trường này, mặc định thử SearXNG local. Có thể chạy theo [hướng dẫn SearXNG](../scripts/searxng/README.md). Lỗi kết nối, HTTP 202, JSON sai hoặc kết quả rỗng chuyển sang DuckDuckGo HTML rồi Bing RSS. Có thể đặt `fallback_search_urls` thành danh sách URL template hoặc `[]` để tắt fallback. Các truy vấn fallback được gửi đến dịch vụ tìm kiếm công cộng. Bing RSS có thể trả kết quả kém liên quan hoặc bị giới hạn.

```powershell
python -m apps.cli.main direct --preset planner "Bản CPython ổn định mới nhất và ngày phát hành là gì? Đối chiếu trang Downloads và trang phát hành chính thức."
```

Preset ví dụ là vai trò `planner` nếu đã cấu hình. Dùng `python -m apps.cli.main presets` để xem preset trên máy. Câu trả lời Direct ghi thêm danh sách **Trang đã đọc** từ URL do công cụ ghi nhận. Đây là dấu vết truy xuất, còn việc một trang có hỗ trợ từng khẳng định vẫn cần đối chiếu nội dung.

## Các công cụ

| Công cụ | Hành vi |
| --- | --- |
| `research__web_search` | Trả tiêu đề, URL, đoạn trích và nguồn tìm kiếm. |
| `research__web_scrape` | Đọc HTML, văn bản hoặc JSON công khai. `research__web_fetch` là tên tương thích cũ. |
| `research__web_research` | Tìm một chủ đề và đọc tối đa ba miền trong kết quả để model đối chiếu. |
| `research__browser_open` | Dựng một trang trong Chromium dùng một lần, lưu ảnh chụp dạng văn bản cho lượt hiện tại. |
| `research__browser_extract` | Đọc lại ảnh chụp văn bản trong lượt hiện tại. |

Trafilatura được dùng khi đã có trong môi trường Python. Nếu chưa có, SHAMS dùng bộ trích xuất HTML tích hợp. Browser mặc định tắt. Để bật, cần Node, Playwright phiên bản hỗ trợ `routeWebSocket`, và Chromium tương thích đã cài. Thêm vào mục `research`:

```json
"browser": {
  "enabled": true,
  "module": "C:/path/to/node_modules/playwright",
  "executable": "C:/Program Files/Google/Chrome/Application/chrome.exe"
}
```

`module` mặc định là `playwright` nếu Node phân giải được package. Khi không chỉ định `executable`, Playwright dùng Chromium đã có của nó. Browser chỉ tải tài liệu, script và dữ liệu GET cùng origin qua transport Research. Trang phụ thuộc CDN khác origin, đăng nhập, cookie, CAPTCHA, WebSocket, thao tác nhập liệu hoặc mạng nội bộ có thể không đọc được.

## Giới hạn truy cập

- Mỗi lượt có ngân sách số lần gọi, số lần tìm, dữ liệu đưa vào context và thời gian dành cho công cụ. Timeout của lần sinh văn bản do provider quản lý riêng.
- Transport không dùng proxy môi trường, không gửi cookie hay thông tin xác thực. Nó kiểm địa chỉ trước từng kết nối và redirect, rồi kết nối đến IP đã kiểm. Chỉ request đầu đến endpoint tìm kiếm đã cấu hình mới được dùng loopback.
- Văn bản web luôn được đánh dấu là dữ liệu không đáng tin. Sau khi gọi Research trong một lượt, `ToolBox` khóa MCP trong phần còn lại của lượt đó. Cơ chế này giới hạn công cụ đang cấp, còn mô hình vẫn có thể diễn giải sai nội dung hoặc sinh đầu ra cần QC.
