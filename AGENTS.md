# AGENTS.md: bàn giao cho agent tiếp theo (Codex / Claude)

Cập nhật: 2026-11-06. Nhánh làm việc: `local-models` (PR #6). HEAD lúc bàn giao: `2d30505`.
Chủ dự án: Ropy (GitHub `adbosp`). Trả lời bằng **tiếng Việt**.

## 1. SHAMS là gì

SHAMS (Sequential Heterogeneous AI Manufacturing System) là hệ thống sản xuất phần mềm/game bằng nhiều model AI
chạy local, **dùng chung cho mọi loại dự án** (không phải công cụ cho một game). Bộ điều phối Python chạy trên CPU
và lập Task DAG. Mỗi task được giao cho worker theo capability, đi qua QC độc lập, sửa lỗi theo vòng lặp có giới hạn,
rồi đưa vào artifact registry.

- `shams/`: lõi, gồm scheduler, state machine, event store SQLite, QC, repair router, model registry và workers.
  **Lõi không được nhắc tên tool hay engine cụ thể**; có test kiểm tra điều này.
- `shams_packs/`: domain pack (`game`, `software`), engine profile (`engines.py`) và template DAG (`templates/*.json`).
- `shams_tools/`: extension cho tool thật, gồm ComfyUI, SF3D, Blender, `scene_json` (model 3D viết JSON, SHAMS dựng
  GLB) và `web_build` (Vite build + smoke test headless qua CDP).
- `apps/api/server.py`: FastAPI + UI web (`apps/ui/index.html`, một file). `apps/cli/main.py`: lệnh `shams`.
- Thiết kế và ADR nằm trong `docs/design/`. `CHANGELOG.md` mục Unreleased liệt kê mọi thay đổi gần đây.

## 2. Quy tắc bắt buộc của chủ dự án

1. Không tự đổi định hướng; không trộn SHAMS với các game khác của Ropy (Stickman Tăng Ca, SundayGame, Viễn Đông City).
2. Giữ template, UI và ví dụ trung lập, không gắn với một dự án cụ thể.
3. Nói rõ cái gì **đã có** và cái gì **mới là đề xuất**. Không báo thành công nếu chưa thực sự chạy.
4. **Làm xong mỗi thay đổi thì deploy lên PC và chạy test**: trên ROPY-PC vào `C:\AI\SHAMS`, `git pull`, rồi
   `pytest`. Báo kết quả chạy trên máy, không chỉ kết quả CI.
5. Không ghi API key vào file. Không hard-code model ID trong phần điều phối.
6. Không xoá model, checkpoint hay venv. Không tải model lớn khi chưa hỏi.
7. Cài phần mềm từ internet lên PC chỉ khi Ropy **tự gõ** lời đồng ý.
8. Không bao giờ chạy preset 18+ hay NSFW LoRA khi Ropy chưa tự chọn. Preset 18+ không bao giờ chạy tự động.
9. Không nhận task HUMAN_REVIEW thay Ropy (trừ chế độ autonomous, nơi autopilot đã được Ropy cho phép).
10. Bỏ qua tạm các worker còn thiếu (ví dụ âm thanh); Ropy sẽ thêm sau.

## 3. Máy chạy thật: ROPY-PC

- Windows 11, RTX 3060 12 GB, 32 GB RAM. Repo ở `C:\AI\SHAMS` (venv py3.12), workspace dự án ở `C:\AI\SHAMS_Projects`.
- Settings người dùng: `C:\Users\ROPY\.shams\settings.json` (có bản sao lưu `*.bak-*`).
- Ollama: `qwen3.5:9b` (planner), `qwen2.5-coder:14b` (coder, **viết code game**),
  `hf.co/ccore/Qwen3.5-4B-3D-Coder:Q8_0` (scene3d, **chỉ dựng 3D** bằng JSON primitives), `qwen3-vl:4b` (visual QC).
- Role `image` (SDXL Lightning) và `image_to_3d` (Stable Fast 3D) **đã tắt theo yêu cầu**; file model vẫn còn trên ổ đĩa.
- Toolchain web (Vitest, Three.js, Vite) nằm ở `C:\Users\ROPY\.shams\toolchains\web` (`shams toolchain install web`).
- Node 24, VS2022 C++, WebView2 đã có. Rust: Ropy đã gõ "đồng ý cài Rust" (2026-11-06); PC đang cài rustup stable-msvc. Kiểm tra `rustc --version` trước khi build client.
- Chạy app: `shams ui C:\AI\SHAMS_Projects`, mở http://127.0.0.1:8765.

## 4. Trạng thái hiện tại (đã có, đã test)

- **Template game `vertical_slice_3d`** có 12 task:
  - scaffold, player_controller, spawner, core_mechanic, hud;
  - `character_brief`: planner viết 3 mô tả tiếng Anh chi tiết (player, npc, environment) và giải thích tiếng lóng
    ("cưa gái" là tán tỉnh);
  - player/npc/environment model: `mesh.procedural`, builder `json`, do scene3d dựng;
  - sfx, integration, build_game.
- **Định tuyến**: code game dùng capability `code.game.*` / `integration.game`. Coder (`code.*`) nhận các task này,
  trừ khi đặt `models.roles.game_coder`.
- **Scheduler** chọn worker khớp nhất (`NodeRegistry.preferred`) hoặc worker ghi trong `provider_hint`. Hỏng 3 lần thì
  repair router chuyển sang worker khớp kế tiếp và cho thêm 2 lần thử.
- **Chế độ autonomous** là mặc định: task hết lượt sửa được autopilot nhận, kèm danh sách check FAIL; tiêu chí không
  có checker không chặn run. Task không tạo được file nào vẫn dừng ở HUMAN_REVIEW.
- **Web build**: role `build` mặc định chạy Vite build, smoke test headless (Edge/Chrome qua `shams_tools/js/cdp_smoke.mjs`),
  zip, và thư mục `play/`.
- **Link chơi thử**: khi build được nhận, chat đăng link `/play/<task>/` (redirect sang một static server riêng cho
  mỗi build, phục vụ từ thư mục gốc) và tự mở trình duyệt (`general.open_game`).
- **Direct Mode** dạng chat: ảnh, video, âm thanh hiện ngay trong chat; file khác hiện thành thẻ tải về; Markdown và
  khung code có nút sao chép.
- **Settings > Chung** có công tắc preset 18+ (`general.allow_adult_content`, mặc định tắt) và tự mở game.
- **Xoá dự án** (biểu tượng thùng rác) là **vĩnh viễn** và được ghi vào `<workspace>/_logs/deletions.log`.
  Ropy đã tự xoá real14 và real15. `C:\AI\SHAMS_Projects\_trash` chứa 14 dự án cũ từ bản trước.
- Test: 192 pass (có toolchain web), 189 pass + 3 skip khi không có toolchain. PC đã chạy 192 pass ở `2d30505`.

## 5. Việc tiếp theo (chưa làm)

1. **Client desktop thay cho web**, theo kiểu Codex / Claude Desktop (học trải nghiệm, không sao chép thương hiệu).
   - Code Tauri 2 + React nằm ở PR #7, nhánh `claude/project-thread-e6mtut` (gồm `apps/desktop`, `apps/api/desktop`,
     ADR-122, `docs/DESKTOP.md`).
   - Cần rebase lên `local-models` và đưa các tính năng mục 4 vào client (Direct Mode dạng chat, link chơi thử,
     công tắc Settings, xoá dự án, autonomous).
   - Build trên PC sau khi có Rust: `scripts/desktop-build.ps1`.
2. **Chạy thử game mới** với template 12 task ở chế độ autonomous, mục tiêu "tạo cho tôi một game cưa gái tại 1 khu
   vườn." trên Web/Three.js. Kiểm tra brief chi tiết có giúp nhân vật đúng màu và đúng giới tính không, NPC có hiện
   trong cảnh không, rồi mở game cho Ropy chơi. Chỉ chạy khi Ropy yêu cầu.
3. **Đã biết còn yếu**:
   - Model 4B dựng nhân vật thành các khối rời và từng làm mất đầu nhân vật chính. Khu vườn thì dựng ổn.
   - Game real13 thiếu cơ chế tán tỉnh thật. Có thể thêm task: lại gần NPC, hội thoại, điểm thiện cảm, điều kiện thắng.
   - Smoke test chưa bắt lỗi 404 khi game dùng đường dẫn tuyệt đối; link chơi thử đã né lỗi này bằng server gốc.
4. **SearXNG** (tìm web qua Google): Ropy đã đồng ý cài. Cần Docker Desktop đang chạy, xem `scripts/searxng/README.md`.
   Sau đó đặt `research.search_url = http://127.0.0.1:8888/search?q={query}&format=json&engines=google`.
5. Ý tưởng từ PC, chưa được duyệt: hỏi lại người dùng khi mô tả còn thiếu, và hiện brief mở rộng để sửa trước khi chạy GPU.

## 6. Cách làm việc

- Chạy test: `.venv/bin/python -m pytest -q`; thêm `SHAMS_TOOLCHAINS=<dir>` để chạy cả test Vitest và trình duyệt headless.
- Mỗi thay đổi: viết hoặc sửa test, ghi `CHANGELOG.md`, commit vào `local-models`, push, rồi deploy và chạy pytest trên PC.
- Thay đổi UI: chụp màn hình bằng Playwright ở khổ máy tính và điện thoại; không được có thanh cuộn ngang.
