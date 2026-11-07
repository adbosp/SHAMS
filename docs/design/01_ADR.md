# SHAMS — Architecture Decision Records (ADR)

**SHAMS = Sequential Heterogeneous AI Manufacturing System**
Phiên bản thiết kế: `v1.1-design` · Ngày: 2026-11-05
Nguồn gốc: `AI_Game_Studio_Chat_Export/` (v1-proposal). File này giữ nguyên định hướng V1, chỉ ghi lại quyết định và các tối ưu bổ sung.

Quy ước trạng thái: **Accepted** = đã chốt từ thảo luận gốc · **Accepted (v1.1)** = tối ưu mới trong bản thiết kế này, không đổi định hướng · **Open** = chờ spike/benchmark.

---

## Phần A — Quyết định gốc (giữ nguyên)

| ID | Quyết định | Trạng thái | Điều kiện xem xét lại |
|---|---|---|---|
| ADR-001 | LangGraph làm nền orchestration (graph cố định trong code, task list động trong DB) | Accepted | Spike S1 cho thấy không đáp ứng resume/interrupt |
| ADR-002 | Deep Agents làm harness cho LLM worker | Accepted (ADR-116: là một loại worker plugin, không phải đường duy nhất) | Spike S1: tỉ lệ tool-call hợp lệ < 80% với mọi model local |
| ADR-003 | Ollama / llama.cpp cho LLM local | Accepted (ADR-116: mọi server OpenAI-compatible và API đều gắn được) | — |
| ADR-004 | ComfyUI cho image; adapter riêng cho 3D/audio/motion | Accepted | — |
| ADR-005 | SQLite + LangGraph checkpointer + Git + artifact registry | Accepted (làm rõ ở ADR-104, ADR-105) | > 1 máy chạy worker |
| ADR-006 | Python + FastAPI backend; Tauri + React + TS desktop (Phase 4) | Accepted | — |
| ADR-007 | Unity trước, Unreal sau qua engine adapter | Accepted (mở rộng ở ADR-115: Unity, Unreal, Godot, Web qua Engine Profile) | — |
| ADR-008 | Local-first; external API là fallback có budget và cần bật | Accepted (mở rộng ở ADR-114: "local" = node do người dùng sở hữu, không nhất thiết cùng máy controller) | — |
| ADR-009 | Pin version LangGraph/Deep Agents, bọc adapter, chỉ fork khi không có extension API | Accepted | — |
| ADR-010 | Không có bằng chứng thì không PASS; thiếu tool thì `NOT_VERIFIED` | Accepted | — |
| ADR-011 | Worker không tự QC; chỉ QC được đặt `ACCEPTED` | Accepted | — |
| ADR-012 | Human approval cho hành động phá hủy, tốn phí, ra ngoài, không đảo ngược | Accepted | — |
| ADR-013 | CCGS chỉ dùng làm tham khảo quy trình/role/QA | Accepted | — |

## Phần B — Tối ưu v1.1 (không đổi định hướng)

### ADR-101 — Một state machine duy nhất cho Task và Artifact
**Vấn đề:** Tài liệu gốc dùng cả `QC_PENDING` (JSON) và `READY_FOR_QC` (mục Handoff); thiếu trạng thái chờ dependency, `STALE`, và thất bại cuối.
**Quyết định:** `QC_PENDING` là **trạng thái task**; `READY_FOR_QC` là **giá trị `status` trong handoff** (lời khai của worker). Thêm `PENDING`, `LEASED`, `STALE`, `FAILED`. Chi tiết: `02_State_Machine.md`.

### ADR-102 — Hai tầng điều phối: Scheduler thuần Python + LangGraph "Task Runner Graph"
**Vấn đề:** Một graph LangGraph khổng lồ chứa cả DAG động thì khó resume, khó debug, khó chạy song song CPU/GPU.
**Quyết định:**
- **Studio Scheduler** (Python deterministic, không LLM): đọc task DB, chọn task READY, cấp lease tài nguyên, khởi chạy run.
- **Task Runner Graph** (LangGraph, graph cố định): vòng đời một task `prepare_context → acquire → load_model → execute → persist_handoff → release → qc → route`. Mỗi task attempt = một `thread_id` LangGraph riêng.
- **Planning Graph** và **Repair Graph** là graph LangGraph riêng, cũng được scheduler gọi như task.
**Lợi ích:** đúng nguyên tắc "graph cố định, task list động"; crash một task không làm hỏng các task khác; LangGraph vẫn lo checkpoint/interrupt/resume.

### ADR-103 — Model-affinity scheduling thay cho keep-warm thụ động
**Vấn đề:** Keep-warm chỉ có lợi nếu task kế tiếp *tình cờ* cùng model.
**Quyết định:** Scheduler chủ động ưu tiên task READY dùng **model đang nạp**, có chống đói (tuổi chờ tối đa, số task liên tiếp tối đa). Chi phí chuyển model lấy từ số đo thực tế (Spike S3). Chi tiết: `04_Scheduling_and_Policies.md §1`.

### ADR-104 — Event log là nguồn sự thật; bảng task là projection
**Vấn đề:** SQLite task DB, LangGraph checkpoint, Git cùng giữ trạng thái → nguy cơ lệch nhau sau crash.
**Quyết định:**
- `events` (append-only, SQLite, transaction) là **nguồn sự thật duy nhất** cho trạng thái task/artifact.
- Bảng `tasks`, `artifacts` là projection cập nhật trong **cùng transaction** với event.
- LangGraph checkpoint chỉ giữ **tiến độ bên trong một attempt** (đang ở node nào). Khi lệch, event log thắng.
- Một tiến trình duy nhất ghi DB (scheduler/API process); worker trả kết quả qua scheduler.

### ADR-105 — Artifact store content-addressed; Git chỉ cho text/code
**Vấn đề:** Đưa mesh/texture/audio vào Git làm repo phình và chậm.
**Quyết định:** Mọi artifact lưu tại `artifacts/sha256/<ab>/<hash>` (bất biến) + bản ghi trong `artifacts` table. Workspace dự án game (Unity project) là Git repo cho code/scene/prefab/meta; asset nhị phân lớn được **import từ artifact store** vào workspace và theo dõi bằng Git LFS hoặc `.gitignore` + manifest hash. Mỗi task attempt thay đổi code chạy trên **Git branch/worktree riêng**; merge khi `ACCEPTED`.

### ADR-106 — QC theo bậc rẻ trước, đắt sau, dừng sớm
**Quyết định:** Mỗi gate chạy theo thứ tự: (1) schema/hash/file tồn tại → (2) compiler/validator → (3) test tự động → (4) chạy scene + screenshot/log → (5) review bằng model (semantic/visual). Bậc nào FAIL thì dừng, không nạp model review. Model review chỉ chạy khi acceptance criteria có loại `semantic` hoặc `visual`.

### ADR-107 — Phân loại lỗi deterministic trước, Analysis LLM sau
**Vấn đề:** Gọi Analysis Agent cho mọi FAIL tốn một lần đổi model GPU.
**Quyết định:** `FailureClassifier` dùng quy tắc (regex lỗi compiler Unity, test name, validator code) để gán `failure_class` và route thẳng tới worker sửa. Chỉ khi `failure_class = unknown` hoặc lặp lại ≥ 2 lần với cùng chữ ký lỗi mới gọi Analysis Agent.

### ADR-108 — Planner dùng DAG template + LLM điền tham số
**Quyết định:** Có thư viện DAG template theo loại sản phẩm (V1: `unity_3d_vertical_slice`). Planner LLM chọn template và điền/khai triển task; validator kiểm tra chu trình, contract, capability. Yêu cầu không khớp template → Planner tự do tạo DAG, bắt buộc qua validator và (tùy chế độ) human approval.

### ADR-109 — Engine/editor là tài nguyên GPU cần lease
**Quyết định:** Unity/Blender/UE khi chạy chế độ có đồ họa (play mode, screenshot, render) được coi là **consumer GPU** và phải lấy lease như model. Unity `-batchmode -nographics` (compile, EditMode test, build) chạy làn CPU, song song được với inference.

### ADR-110 — Dependency invalidation theo hash, revalidate trước rồi mới rework
**Quyết định:** Mỗi handoff ghi hash của input đã dùng. Khi artifact upstream được chấp nhận với hash mới, mọi task downstream `ACCEPTED` dùng hash cũ → `STALE`. Task `STALE` đi **QC lại trước**; chỉ khi QC FAIL mới tạo repair task. Tránh làm lại hàng loạt.

### ADR-111 — Side-effect ledger cho idempotency
**Quyết định:** Mọi side effect ngoài workspace (API trả phí, publish, push) có `idempotency_key = hash(task_id, attempt, action, input_hashes)` ghi vào `side_effects` table **trước** khi gọi và cập nhật kết quả sau. Khi resume, key đã `COMPLETED` thì không gọi lại.

### ADR-112 — Human Review có từ Phase 1 qua CLI/API
**Quyết định:** Trước Tauri UI (Phase 4), approval và human review đi qua FastAPI endpoint + CLI `shams review`. LangGraph `interrupt()` dùng để dừng chờ duyệt.

### ADR-113 — Chế độ vận hành
**Quyết định:** 3 chế độ: `collaborative` (duyệt kế hoạch và mọi gate quan trọng), `guided` (duyệt kế hoạch + hành động rủi ro), `autonomous` (chỉ duyệt hành động thuộc ADR-012). Mặc định `guided`.

### ADR-114 — Controller độc lập phần cứng; model chạy trên Compute Node (thay giả định RTX 3060)
**Vấn đề:** Bản v1-proposal gắn toàn bộ hệ thống với một máy (Win11, RTX 3060 12 GB). Nhưng SHAMS về bản chất là **bộ điều khiển**; model AI là phần gắn vào sau.
**Quyết định:**
- **Studio Controller** (Scheduler, Event Store, Artifact Registry, QC logic, API/CLI/UI) chỉ cần CPU, chạy được trên **PC bất kỳ** (Windows/Linux/macOS, không cần GPU). Yêu cầu tối thiểu tham khảo: 4 nhân, 8 GB RAM, Python 3.11+.
- Mọi thứ cần tài nguyên đặc biệt nằm sau abstraction **Compute Node**: một node khai báo tài nguyên của nó (số GPU, VRAM mỗi GPU, CPU slots, engine cài sẵn) và các provider nó phục vụ. Node có thể là:
  - `local` — chính máy controller (có GPU hoặc không);
  - `remote` — máy khác trong mạng LAN chạy Ollama/llama.cpp/ComfyUI/Unity, nối qua HTTP (và Node Agent nhỏ ở Phase 2+);
  - `external` — API bên thứ ba (không có VRAM, chỉ có budget/rate limit).
- Scheduler không còn "1 GPU lane cố định": **mỗi GPU trên mỗi node là một lane**; mỗi node có lane CPU riêng. Affinity (ADR-103) tính theo **(node, gpu, model)**.
- Capability Registry ghi `node_id` cho mỗi worker; router chọn worker theo capability, rồi theo node còn rảnh.
- RTX 3060 12 GB chỉ còn là **hồ sơ tham chiếu** (`profile.rtx3060_12gb`) để đặt mặc định và benchmark, không phải yêu cầu.
- Hệ thống phải chạy được end-to-end **khi chưa gắn model nào**: chế độ `mock` dùng worker giả (trả artifact mẫu + handoff hợp lệ) để kiểm thử orchestrator, state machine, QC, repair, crash/resume.
**Hệ quả:** ADR-109 áp dụng theo node (engine có đồ họa lấy lease GPU *trên node đó*). Spike S1/S3 chuyển thành việc của lúc gắn model đầu tiên, không chặn việc xây controller.

### ADR-115 — Đa engine qua Engine Profile + Engine Adapter (mở rộng ADR-007)
**Vấn đề:** ADR-007 chọn "Unity trước, Unreal sau qua engine adapter", nhưng template, capability, đường dẫn và contract trong Phase 1a đều viết cứng cho Unity. Người dùng cần tạo game cho Unity, Unreal Engine 5, Godot và web (Three.js + WebGPU).
**Quyết định:**
- Template DAG viết **một lần, trung lập engine** (`vertical_slice_3d`), dùng token `${code}`, `${scripts}`, `${script_contract}`, `${build}`… Khi lập kế hoạch, template được **instantiate cho một Engine Profile**; `dag.template` ghi `vertical_slice_3d@<engine>`.
- **Engine Profile** (`studio/engines`) khai báo: ngôn ngữ, capability code/test/build, media type và contract cho script/test/scene/mesh/build, cây thư mục (scripts, tests, scenes, models, textures, audio, builds), build target.
  - `unity`: C#, `Assets/**`, Unity Test Framework, build Windows.
  - `unreal`: C++, `Source/Game`, `Content/**`, Automation Test, RunUAT BuildCookRun Win64; asset nhập qua FBX/glTF, không sinh `.uasset` trực tiếp.
  - `godot`: GDScript, `scripts/`, GUT, `--export-release`.
  - `web`: TypeScript + Three.js `WebGPURenderer` (tự lùi WebGL2), Vite build, Vitest, smoke test + screenshot bằng Playwright.
- **Engine Adapter** dựng argv cho compile / test / build / capture của từng engine. Chạy thật và parse log là việc của Spike S2 (mở rộng cho cả 4 engine); trong Phase 1a các bậc QC 2–4 vẫn dùng mock checker.
- Engine chọn lúc tạo project (`shams init --engine`), hoặc đổi trước khi có plan (`shams plan --engine`, UI chọn engine, hoặc nhận diện từ câu chat: "UE5", "Godot", "Three.js/WebGPU"…). **Không đổi engine sau khi đã có plan** (artifact đã sinh không chuyển engine được); muốn đổi thì tạo project mới.
- Asset chung (texture, audio, mesh glTF) dùng chung contract; chỉ script/scene/test/build là riêng theo engine.
**Hệ quả:** Worker code cần capability theo ngôn ngữ (C#, C++, GDScript, TS); Benchmark Lab đo riêng từng capability. Unity vẫn là engine mặc định. Template cũ `unity_3d_vertical_slice` giữ làm alias.

### ADR-116 — Lõi mở: mọi model local, mọi API, engine/template/checker tùy biến (plugin)
**Vấn đề:** SHAMS sẽ được phát hành cho người khác dùng như một hệ thống sản xuất chung (tương tự Claude Code), không gắn với một game, một model hay một nhà cung cấp. Bản 1a chỉ có Mock Worker; worker thật báo "chưa hỗ trợ".
**Quyết định:**
- **Provider** = một giao diện `chat(messages, model)` cho mọi backend. Có sẵn:
  - `openai_compatible` (mọi server nói OpenAI Chat Completions);
  - preset `ollama` (có load/unload qua `keep_alive`), `llamacpp`, `lmstudio`, `vllm`, `localai`;
  - preset API `openai`, `openrouter`, `groq`, `deepseek`, `mistral`, `together`, `xai`, `gemini`;
  - `anthropic` và `command` (chương trình local bất kỳ).
- **Worker** dùng provider:
  - `LLMWorker` sinh output dạng text (code, scene, config) từ reply JSON;
  - `CommandWorker` gọi chương trình bất kỳ (sinh ảnh/3D/âm thanh, Blender, CLI engine): chương trình đọc `$SHAMS_CONTEXT` và tự ghi output;
  - worker riêng của người dùng khai báo qua `impl = "package.module:Class"`.
  - Mọi worker trả cùng một handoff, nên QC, repair loop, event log, crash/resume không đổi.
- **Cấu hình** `nodes.toml` (trong project hoặc `~/.shams/`) khai báo node, lane và worker ở dạng ngắn (`id, provider, model, node, lane, capabilities, vram_mb, config`). **Khóa API không bao giờ ghi vào file**: `api_key_env` chỉ là tên biến môi trường. Server local (localhost) được gọi trực tiếp, bỏ qua proxy.
- **Model Lifecycle** có adapter `mock` / `none` / `provider` (gọi `load`/`unload` của provider nếu có, ví dụ Ollama). Plugin có thể thêm adapter.
- **Engine tùy biến**: file `engines/<name>.toml` (trong project hoặc `~/.shams/`) hoặc entry point `shams.engines`. Profile khai báo luôn lệnh compile/test/build/capture (`[engine.commands]`). Template trung lập engine tự áp dụng cho engine mới.
- **Template tùy biến**: thư mục `templates/` trong project hoặc `~/.shams/`, hoặc entry point `shams.templates`, hoặc đường dẫn `.json`.
- **Checker thật**: `register_checker(kind, fn)` hoặc entry point `shams.checkers`. Kind nào chưa có checker thì vẫn là `NOT_VERIFIED` (ADR-010), không bao giờ tự PASS.
- **Plugin** dùng Python entry points (`shams.providers`, `shams.workers`, `shams.engines`, `shams.checkers`, `shams.templates`): cài một gói `pip install shams-plugin-x` là đủ, không sửa mã SHAMS.
- **Artifact** định danh bằng (hash nội dung, logical_id). Hai output trùng nội dung vẫn là hai artifact; blob dùng chung.
**Hệ quả:**
- Mock vẫn là chế độ mặc định khi chưa có model.
- Deep Agents (ADR-002) trở thành một loại worker plugin cho tác vụ cần tool-call nhiều bước, không còn là đường duy nhất.
- Chất lượng mỗi worker vẫn đo bằng Benchmark Lab.
- Project tạo trước thay đổi khóa artifact cần tạo lại (Phase 1a, chưa có dữ liệu thật).

### ADR-117 — Domain Packs: lõi SHAMS không biết lĩnh vực (SHAMS V2.0)
**Vấn đề:** SHAMS là hệ thống sản xuất chung (phần mềm, game, 3D, lĩnh vực tùy biến). Bản trước còn để engine game, template game và mẫu lỗi Unity/Unreal/Godot trong lõi, namespace Python là `studio`.
**Quyết định:**
- Namespace Python đổi sang `shams` (OPEN-8 trong SHAMS_ARCHITECTURE_V2). Lớp chính là `Shams` (`Studio` còn là alias). Project cũ (`studio.sqlite`, cột `engine`) vẫn mở được và được hiểu là domain `game`.
- **Domain Pack** (`shams.packs.DomainPack`) mang toàn bộ kiến thức lĩnh vực:
  - lựa chọn cấu hình (`options`: stack, engine…), `default_config`, `normalize_config`, `configure`;
  - nhận diện yêu cầu (`match_score`, `detect`), template và token, nhãn template;
  - `domain_context` gửi cho worker, `failure_rules` thêm vào bộ phân loại lỗi, `checkers`, log lỗi cho Mock Worker, nhãn worker, media type riêng, alias template cũ, plugin dạng file (engine `.toml`).
- Lõi chỉ thấy `domain` + `domain_config`. Project lưu hai cột này; context package có `domain` + `domain_context` thay cho `engine`; task DAG ghi `domain`.
- Pack được tìm theo thứ tự: `register_pack()` trong code → entry point `shams.packs` (`pip install`) → pack có sẵn trong `shams_packs/` (nạp lười theo tên, lõi không import).
- Pack có sẵn:
  - **Software Pack** (mặc định): stack Python, TypeScript (Node.js); template `software_basic` (scaffold → module lõi → điểm vào → tích hợp → đóng gói).
  - **Game Pack**: các engine của ADR-115 (Unity, Unreal Engine 5, Godot, Three.js/WebGPU, engine tùy biến) và template `vertical_slice_3d`.
- Domain được nhận diện từ yêu cầu (pack có điểm cao nhất), hoặc chọn bằng `shams init --domain … --opt key=value`, UI hoặc `POST /api/configure`. Chỉ đổi được trước khi có plan.
- Test bắt buộc: lõi (`shams/`) không import `shams_packs` và không nhắc tên engine/tool/game cụ thể.
**Hệ quả:**
- Thêm lĩnh vực mới (3D, tài liệu, dữ liệu…) = viết một pack, không sửa lõi.
- `shams engines` vẫn còn nhưng thuộc Game Pack; lệnh chung là `shams packs`.
- Checker thật cho Software Pack (pytest, tsc, vitest) đến ở V2.2; trước đó compile/test kết thúc `NOT_VERIFIED` nếu chưa đăng ký checker.
- Intent & Scope Manager, Reuse Before Generate, Fidelity Tier, Generation Budget (SHAMS V2) xây trên lớp pack này, bắt đầu từ V2.1.

### ADR-118 — Ứng dụng chính thức: phần cứng thật, Settings nhiều lớp, MCP, hooks
**Vấn đề:** Bản trước còn VRAM mô phỏng, chế độ demo khi mở file HTML, cấu hình model chỉ qua `nodes.toml` viết tay, chưa có MCP. SHAMS được phát hành cho người khác nên cần tinh chỉnh được như Claude Code.
**Quyết định:**
- **Phần cứng thật** (`shams.hardware`): GPU qua `nvidia-smi` (NVIDIA), `rocm-smi` (AMD/Linux), registry Windows (mọi GPU, chỉ tổng VRAM), `sysctl` (Apple Silicon, bộ nhớ hợp nhất); RAM và CPU từ hệ điều hành. Không thêm thư viện. UI và `shams hardware` hiển thị số đo thật; không có GPU thì báo rõ, SHAMS vẫn chạy (ADR-114). Worker gắn vào `node.local` mà chưa khai báo node sẽ tự dùng phần cứng thật.
- **Settings nhiều lớp** (`shams.settings`), sau thắng trước: mặc định → người dùng `~/.shams/settings.json` → dự án `<project>/.shams/settings.json` → máy này `<project>/.shams/settings.local.json` (tự gitignore). Đọc thêm `nodes.toml` và `.mcp.json` (định dạng `mcpServers` của Claude Code). Khóa: `general`, `permissions`, `connections`, `nodes`, `workers`, `mcp_servers`, `hooks`, `env`.
- **Bí mật không bao giờ ghi xuống file**: lưu settings bị từ chối nếu có giá trị bí mật trực tiếp; chỉ chấp nhận `api_key_env` hoặc `${VAR}`. Khóa nhập trên UI chỉ nằm trong bộ nhớ tiến trình (`/api/secret`).
- **Connections**: kết nối model/API dùng lại (provider + base_url + api_key_env); worker trỏ `connection` thay vì lặp cấu hình. UI kiểm tra kết nối và liệt kê model (`list_models` cho OpenAI-compatible, Ollama, Anthropic).
- **MCP** (`shams.mcp`): client JSON-RPC không phụ thuộc thư viện, transport `stdio` và Streamable HTTP (JSON hoặc SSE, `Mcp-Session-Id`). Tool lộ ra cho model dưới tên `mcp__<server>__<tool>`. `LLMWorker` có vòng gọi tool (giới hạn `max_tool_rounds`) với provider hỗ trợ tool calling (OpenAI-compatible, Anthropic). Mỗi lần gọi tool được ghi vào `decisions` của handoff.
- **Quyền** (giống Claude Code): `default` (chỉ tool `readOnlyHint` + danh sách `allow`), `full_access` (mọi tool trừ `deny`), `read_only`. Mẫu glob trên tên tool; `deny` luôn thắng. Tool bị từ chối trả lỗi cho model, không dừng pipeline.
- **Hooks**: lệnh chạy khi có sự kiện (glob trên loại event, thêm `run.finished`); nhận JSON qua stdin và `SHAMS_EVENT`. Hook lỗi ghi `hook.failed`, không dừng pipeline.
- **Ứng dụng**: `shams ui [workspace]` mở thư mục làm việc (mặc định `~/SHAMS`) chứa nhiều dự án, tự mở trình duyệt; tạo/chuyển dự án trên UI; câu chat đầu tiên tự tạo dự án. Bỏ chế độ demo trong UI: không có server thì hiện hướng dẫn chạy `shams ui`. Chế độ thử (mock) là lựa chọn rõ ràng trong Settings (`general.mock`: tự động khi chưa có worker / bật / tắt).
- Phát hành: workflow `release.yml` dựng wheel + sdist và GitHub Release khi đẩy tag `v*` khớp version gói.
**Hệ quả:**
- Mọi tinh chỉnh làm được trên UI (8 tab Settings) hoặc bằng file JSON; CLI có `shams hardware`, `shams settings`, `shams mcp`.
- Người dùng tự chịu trách nhiệm khi bật `full_access`; mặc định an toàn.
- Checker thật cho từng pack và Intent & Scope Manager vẫn là V2.1/V2.2.

### ADR-119 — Ứng dụng desktop: client Tauri + React tách khỏi SHAMS Core Service
**Vấn đề:** SHAMS cần là ứng dụng desktop thật (giống Claude Code/Codex), nhưng lõi Python (LangGraph, worker, GPU) phải chạy tiếp khi đóng giao diện và phục vụ được nhiều client.
**Quyết định:**
- **SHAMS Core Service** (Python) là tiến trình riêng: orchestration LangGraph, Deep Agents + worker chuyên môn, Model Registry, GPU lease, event store, MCP/tool. API hiện có trong `apps/api/server.py` (FastAPI, `127.0.0.1`) chính là Local API của Core Service; không viết lại.
- **Client desktop**: Tauri 2 + React. Tauri khởi động/giám sát Core Service như sidecar (hoặc nối vào service đang chạy), giao tiếp qua Local API (HTTP + SSE/WebSocket). Electron chỉ là phương án dự phòng.
- Client khác (CLI `shams`, web UI hiện tại, remote) dùng cùng Local API.
- Trước khi chọn chính thức phải spike: đóng gói Python (PyInstaller/embedded) làm sidecar, quản lý tiến trình (khởi động, tắt, crash, cổng bận), đóng gói Windows/macOS/Linux.
**Hệ quả:**
- Đóng UI không dừng run; mở lại UI thấy trạng thái từ event store.
- Web UI hiện tại tiếp tục dùng đến khi client Tauri đạt tính năng tương đương.
- Local API cần token cục bộ khi mở cho client từ xa (chưa làm).

### ADR-120 — Model local theo vai trò, công cụ ảnh/3D, QC tài sản thật
**Vấn đề:** Máy người dùng đã có model Ollama, ComfyUI, Stable Fast 3D và Blender. SHAMS cần dùng đúng model cho đúng việc mà không viết ID model vào code, không gắn lõi vào một công cụ, và không báo thành công khi chưa chạy thật.
**Quyết định:**
- **Model Registry theo vai trò** (`shams.models`): vai trò `planner`, `coder`, `scene3d`, `visual_qc`, `embedding`, `image`, `image_to_3d`. Settings `models.roles` ghi model *mong muốn*; registry hỏi Ollama (`/api/tags`, `/api/show`, `/api/ps`) và ánh xạ tên mong muốn sang ID thật (alias → khớp đúng → không phân biệt hoa thường → khớp theo token; nhập nhằng thì báo, không đoán). Mỗi vai trò đã giải được sinh một worker `worker.role.<vai trò>` với capability dạng họ (`code.*`), ước lượng VRAM từ kích thước model + context. LangGraph và scheduler chỉ thấy capability.
- **Ollama dùng API gốc** `/api/chat` (tắt thinking, `num_ctx`, JSON mode, ảnh cho model thị giác). `unload` chờ đến khi `/api/ps` không còn model, nên đổi model tuần tự trên một GPU không bị OOM.
- **Công cụ nằm ngoài lõi** (`shams_tools/`, nạp bằng `plugins.load_extensions()` hoặc entry point `shams.extensions`): ComfyUI (workflow API-format có placeholder, kiểm tra node/file model trước khi chạy, `/free` để trả VRAM), Stable Fast 3D (lệnh `run.py` trong venv riêng), Blender (worker 3D Scene, kiểm tra import GLB). Lõi không nhắc tên công cụ (test ADR-117 vẫn áp dụng).
- **3D Scene worker**: model *coding* viết Blender Python; Blender chạy headless, render bằng chứng, xuất GLB; lỗi được gửi lại model tối đa `max_fix_rounds` lần. Không coi đây là sinh mesh bằng mạng nơ-ron.
- **Thứ tự 3D**: tái sử dụng → primitive → procedural → modular → placeholder → Stable Fast 3D, chỉ khi có ảnh tham chiếu *và* vật thể thực sự phức tạp. Worker SF3D tự từ chối (BLOCKED) nếu điều kiện không đủ.
- **QC thấy file thật**: checker nhận đường dẫn output/input (`wants_outputs`), chọn theo `criterion.check.type`: `glb`, `image`, `scene_report`, `python`, `pytest`, `vlm`, `blender_import`. Không có checker thì vẫn `NOT_VERIFIED`.
- **Planner LLM** (`shams.planning.llm_planner`): model viết DAG, Plan Validator quyết định, lỗi được gửi lại tối đa 3 vòng, sau đó quay về template của Domain Pack.
- **`shams selftest`** chạy 8 nhóm test trên máy thật, kết quả PASS / FAIL / BLOCKED / SKIPPED kèm bằng chứng; thiếu model/công cụ là BLOCKED, không bao giờ là PASS.
**Hệ quả:**
- Đổi model chỉ cần sửa settings; `shams models` cho thấy ánh xạ thật.
- Embedding mới có provider (`Ollama.embed`), chưa nối vào truy hồi ngữ cảnh.
- Nhiều GPU hoặc API: scheduler sẵn có chạy song song theo lane; một GPU: tuần tự.

### ADR-121 — Direct Mode, preset theo registry, chính sách nội dung, research web
**Vấn đề:** Người dùng muốn chọn thẳng một model/preset, nhập prompt và chạy, không cần Planner hay DAG; thêm model ảnh MOSS-Image (có LoRA chỉ dành cho người lớn) và model chat chỉ dùng trực tiếp; cho model dùng Internet để research; cảnh 3D đơn giản không cần Blender.
**Quyết định:**
- **Preset là một mục trong Model Registry** (`models.presets`, và mỗi vai trò đã giải được cũng là một preset). Mỗi preset có: `kind`, `provider`, model thật, `status`, `capabilities`, `execution_modes` (`direct`, `autonomous`), `autonomous_default`, `input_schema`, `outputs`, `params`, `content_policy`, tài nguyên (lane, VRAM). Worker của preset chỉ có capability `direct.<id>`; chỉ khi `autonomous_default` và `autonomous` có trong `execution_modes` thì mới thêm capability thường, nên Planner không bao giờ tự chọn preset chỉ-direct.
- **Direct Mode dùng chung toàn bộ runtime**: `Shams.direct(preset, params)` kiểm tra schema, chính sách, trạng thái rồi tạo **một** task (plan `plan.task.direct.NNNN`, template `direct`) và để scheduler, GPU lease, model lifecycle (nạp/dỡ), worker, Artifact Registry, QC và event log xử lý như mọi task. `run_task()` chạy tới trạng thái cuối. Không có đường chạy riêng.
- **Chính sách nội dung** (`shams.policy`): preset có `content_policy.adult` không bao giờ được Autonomous Mode chọn (settings bị từ chối nếu khai báo `autonomous_default` hoặc chế độ `autonomous`), cần `general.allow_adult_content: true` **và** xác nhận mỗi lần chạy; prompt về trẻ vị thành niên, không đồng thuận/lạm dụng, người thật có thể nhận diện bị từ chối (Anh + Việt); luôn thêm negative prompt chống trẻ vị thành niên. Mọi lần từ chối ghi `policy.refused`. Bộ lọc giảm rủi ro, không phải bảo đảm.
- **Research web** (`shams.research`): hai tool chỉ đọc `research__web_search`, `research__web_fetch` cho model có tool calling; chặn địa chỉ local/private (kể cả sau redirect) nên model không chạm được Ollama/ComfyUI/LAN; giới hạn dung lượng, danh sách cho phép/cấm, ghi log URL; nội dung web được đánh dấu là dữ liệu không tin cậy. Bật bằng `research.enabled`.
- **Cảnh 3D đơn giản không cần Blender**: model 3D viết mô tả cảnh JSON (primitive, vị trí, xoay, màu, camera, đèn); `shams_tools.scene_json` kiểm tra, dựng GLB (POSITION/NORMAL/UV, vật liệu PBR, camera, KHR_lights_punctual) và render z-buffer bằng Python để làm bằng chứng QC. Blender chỉ dùng cho cảnh phức tạp (modifier, boolean, sculpt, rig, terrain...) hoặc khi JSON thất bại sau các vòng sửa.
- **MOSS-Image** qua workflow ComfyUI (`moss_image`, `moss_image_lora`); preflight kiểm tra cả node, file model và giá trị lựa chọn (vd. kiểu text encoder `moss_image` mà ComfyUI cũ chưa có).
**Hệ quả:**
- Thêm model/preset chỉ cần settings; UI (Direct Mode), API (`/api/direct/*`), CLI (`shams presets`, `shams direct`) đọc từ registry.
- `shams selftest` có thêm 3 (cảnh JSON), 3b (Blender), 9a–9e (Direct chat, ảnh, chuyển model, lỗi/chính sách, MOSS). Self-test không bao giờ chạy preset 18+, chỉ preflight.


### ADR-122 — SHAMS Desktop Client: Tauri 2 + React trên Local API, chat stream, agent coding có quyền, GPU gate
**Vấn đề:** Người dùng cần một ứng dụng desktop thật (kiểu Codex / Claude Desktop) để chat trực tiếp với một model, giao việc coding cho agent trong thư mục local, tạo ảnh/video/3D và theo dõi Autonomous Mode, trên cùng runtime SHAMS, không viết lại lõi.
**Quyết định:**
- **Client** `apps/desktop/`: Tauri 2 (Rust) + React + TypeScript + Tailwind. Shell Rust chỉ quản lý vòng đời Core Service: gắn vào backend đang chạy, hoặc chạy `python -m apps.cli.main desktop-backend` (cổng trống + token ngẫu nhiên), chờ `/api/health`, đóng bằng `/api/desktop/shutdown`, chỉ kill tiến trình chính nó tạo. Cấu hình `%APPDATA%\com.shams.desktop\config.json`. Installer NSIS/MSI không chứa model.
- **Local API** thêm `/api/desktop/*` (`apps/api/desktop/`): SQLite riêng (`<workspace>/_desktop/desktop.db`) cho hội thoại, tin nhắn dạng khối (text, thinking, tool, image, video, model3d, links, error), job, phê duyệt, Project local, nhật ký tool. Stream bằng SSE có số thứ tự (nối lại không mất sự kiện). Token cục bộ bắt buộc khi shell chạy backend.
- **Chat** stream thẳng từ provider (`providers.stream_chat`: Ollama NDJSON gốc, OpenAI-compatible SSE). Model lấy từ Model Registry; không có tên model trong code.
- **Agent coding** = vòng lặp tool calling với công cụ workspace (list/read/search/write/edit/delete/run). Ba chế độ quyền: Ask Before Action, Allow Safe Workspace Actions, Autonomous With Guardrails. Ở mọi chế độ: ngoài thư mục Project bị từ chối, thao tác phá hủy/nhạy cảm (xóa, git push/reset, cài đặt, mạng, kill) luôn hỏi. Mỗi lần ghi lưu checkpoint, có diff và hoàn tác theo lượt. Model không báo capability `tools` thì không chạy được như agent (báo lỗi rõ, không giả lập).
- **Media** (ảnh/video/3D) chạy như task Direct Mode (scheduler, Artifact Registry, QC sẵn có). Thêm kind `video` (WebP động) và workflow `wan_t2v_1_3b`, `sdxl_checkpoint`.
- **GPU gate dùng chung cả tiến trình** (`shams.scheduling.gate`): chat, agent, Direct Mode và vòng Autonomous (trừ project chạy thử) lấy gate trước khi dùng GPU; một GPU thì tuần tự. Đổi model qua API của backend (Ollama `keep_alive: 0`, ComfyUI `/free`), không kill tiến trình.
- **Model Manager**: dò Ollama + ComfyUI; danh mục `inventory.json` chỉ là dữ liệu và được so với file thật (installed / missing / incomplete / unknown); đăng ký/bật/tắt/xóa preset vào settings người dùng, preset mới mặc định chỉ Direct.
**Hệ quả:**
- Web UI cũ vẫn chạy; client desktop dùng cùng API.
- Python chưa được đóng gói thành sidecar: app dùng Python/venv có sẵn trên máy (PyInstaller là bước sau).
- Biến môi trường Project lưu dạng chữ thường trong SQLite trên máy (cảnh báo khi tên giống bí mật).

### ADR-123 — Công cụ ngoài qua MCP: danh mục server có sẵn, MCP trong Desktop Chat, không thêm workflow engine thứ hai
**Vấn đề:** Một sơ đồ "Local AI Stack" đề xuất xếp Open WebUI → Ollama + Dify → Goose/Codex → MCP Gateway. Phần lớn trùng với thứ SHAMS đã có: Desktop/Direct Mode (giao diện), Ollama (runtime), orchestrator + event log (workflow), coding agent (worker). Dify sẽ là workflow engine thứ hai, làm mất nguồn sự thật duy nhất, QC, handoff và GPU lease. Phần đáng lấy là tầng MCP. SHAMS đã có MCP client (ADR-118), nhưng người dùng phải tự viết cấu hình server, và Desktop Chat chưa dùng được MCP.
**Quyết định:**
- **Không** tích hợp Dify. Open WebUI chỉ là tùy chọn bên ngoài để chat với Ollama, không phải UI chính. Goose/Codex nếu dùng thì cắm vào như CommandWorker và phải qua GPU gate (chưa làm).
- **Danh mục preset** `shams_tools/mcp_catalog.py`: `searxng`, `browser` (Playwright MCP), `github` (server HTTP chính thức), `blender` (blender-mcp), `unity` (MCP for Unity qua HTTP), `filesystem`. Preset chỉ là một mục `mcp_servers` bình thường, copy vào settings và sửa được như mọi server. Preset chỉ thêm quyền cho tool đọc; tool ghi vẫn phải được cho phép rõ ràng.
- **CLI** `shams mcp --presets`, `shams mcp --add <preset> [--as <tên>]`. **API** `GET /api/mcp/presets`, `POST /api/mcp/presets/{id}`.
- **Desktop Chat** dùng được tool MCP khi `desktop.chat_mcp` (hoặc `params.mcp` của preset/lượt chat) là danh sách server hoặc `"all"`, và model Ollama hỗ trợ tools. Mọi lời gọi vẫn qua `permissions`. Sau khi đã đọc dữ liệu web không tin cậy trong lượt, tool MCP bị khóa (quy tắc sẵn có của ToolBox).
**Hệ quả:**
- Không thêm dependency Python. Server chạy bằng `npx`/`uvx` lúc dùng lần đầu; nên ghim phiên bản trong settings khi đã chạy ổn.
- Preset `blender`/`unity` được đánh dấu `gpu`. Việc giữ GPU gate quanh tool của chúng là bước tiếp theo (`mcp_catalog.gpu_servers`). Chưa có preset UE5 vì chưa có server MCP đủ ổn định.

## Phần C — Còn mở (quyết định bằng spike)

| ID | Câu hỏi | Quyết định bằng |
|---|---|---|
| OPEN-1 | Model code/reasoning mặc định cho từng hồ sơ phần cứng (bắt đầu từ hồ sơ của node đầu tiên được gắn) | Spike S1 + benchmark B-set |
| OPEN-2 | Ngưỡng affinity và chi phí chuyển model | Spike S3 |
| OPEN-3 | Image-to-3D local hay API | Spike Phase 3 |
| OPEN-4 | Sandbox trên Windows (Windows Sandbox / WSL2 / container / chỉ path allowlist) | Trước khi phân phối; V1 dùng path allowlist + Git worktree |
| OPEN-5 | Microsoft Agent Framework thay LangGraph? | Không xét trong V1 trừ khi ADR-001 bị bác bởi Spike S1 |
| OPEN-6 | Tracing local (LangSmith self-host / Langfuse / OpenTelemetry file) | Phase 1; mặc định ghi event log + OpenTelemetry JSON file |
