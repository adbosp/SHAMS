<p align="center"><img src="docs/assets/logo.png" alt="SHAMS" width="96"></p>

# SHAMS — Sequential Heterogeneous AI Manufacturing System

Hệ thống sản xuất bằng AI dùng chung: một **bộ điều khiển** chia việc theo Task DAG, giao từng việc cho model AI chuyên môn phù hợp (code, reasoning, image, 3D, animation, audio), kiểm thử độc lập và sửa lỗi theo vòng lặp có giới hạn để tạo ra sản phẩm hoàn chỉnh: phần mềm, game, hoặc lĩnh vực bạn tự thêm. Không gắn với một lĩnh vực, một model hay một nhà cung cấp: **mọi model local, mọi API đều gắn thêm được** (ADR-116), **mỗi lĩnh vực là một Domain Pack** (ADR-117).

Thiết kế chi tiết: [`docs/design/`](docs/design/README.md) (ADR, state machine, contracts, scheduling, component design, benchmark).

## Trạng thái: SHAMS 2.0 — lõi + Domain Packs + ứng dụng

Controller chạy trên **PC bất kỳ** (không cần GPU, ADR-114). Model AI được gắn sau qua *Compute Node*. Khi chưa có model, **Mock Worker** cho phép chạy đầy đủ vòng đời để kiểm thử orchestrator.

| Thành phần | Trạng thái |
|---|---|
| Event Store (SQLite, nguồn sự thật, ADR-104) | ✅ |
| State machine thống nhất (ADR-101) | ✅ |
| Artifact Registry theo hash + Impact Analyzer STALE (ADR-105, ADR-110) | ✅ |
| Node Registry: lane sinh theo node/GPU (ADR-114) | ✅ |
| Scheduler: affinity theo model, chống đói, lease TTL (ADR-102, ADR-103) | ✅ |
| Task Runner Graph trên LangGraph, checkpoint + crash/resume | ✅ |
| Context Compiler | ✅ |
| QC 5 bậc, dừng sớm (ADR-106) — bậc 1 thật; bậc 2–4 dùng mock checker | ✅ (mock) |
| Failure Classifier + Repair Router + fix_upstream (ADR-107) | ✅ |
| Human review qua CLI (ADR-112), 3 chế độ (ADR-113) | ✅ |
| Side-effect ledger (ADR-111) | ✅ |
| Worker thật qua provider: Ollama, llama.cpp, LM Studio, vLLM, mọi API OpenAI-compatible, Anthropic, lệnh CLI, plugin Python (ADR-116) | ✅ |
| Lõi không biết lĩnh vực; Domain Pack: Software (Python, TypeScript), Game (ADR-117) | ✅ |
| Phần cứng thật (GPU/VRAM, RAM, CPU), Settings nhiều lớp, kết nối model/API, MCP, quyền, hooks (ADR-118) | ✅ |
| Engine / template / checker / pack tùy biến (TOML hoặc entry point) | ✅ |
| Intent & Scope Manager, Reuse Before Generate, Fidelity Tier, Generation Budget (V2.1) | ⏳ |
| Checker thật cho Software Pack: pytest, tsc, vitest (V2.2) | ⏳ |
| Deep Agents worker (tool-call nhiều bước), Analysis Agent LLM, Planner LLM | ⏳ Phase 1b |
| Game Pack: Unity, Unreal Engine 5, Godot, Three.js/WebGPU (ADR-115), template trung lập engine | ✅ (mock) |
| Checker engine thật (compile/test/build/screenshot cho 4 engine; lệnh đã dựng sẵn) | ⏳ Spike S2 |
| Git worktree theo attempt (ADR-105) | ⏳ |
| Ứng dụng desktop SHAMS (Tauri 2 + React, `apps/desktop`): chat stream, Direct AI, agent coding theo Project, Autonomous, Media, Model Manager | ✅ |
| Ứng dụng web cũ (`shams ui`): workspace nhiều dự án, Chat, Workflow, AI Workers, Models, Artifacts, QA, Settings, Console | ✅ |
| SSE thay polling, Node Agent cho máy remote, desktop Tauri | ⏳ Phase 2/4 |

## Chạy thử (mock, không cần GPU)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q

shams packs                                       # software (stack: python | typescript), game (engine: unity | unreal | godot | web)

# Phần mềm (domain nhận diện từ yêu cầu, hoặc --domain software --opt stack=typescript)
shams init projects/notes --project-id notes_cli --goal "CLI Python quản lý ghi chú" --mock
shams plan projects/notes --approve <tên-bạn>
shams run projects/notes

# Game
shams init projects/demo --project-id demo_game --goal "Game đua xe 3D" --domain game --opt engine=godot --mock
shams plan projects/demo --approve <tên-bạn>
shams run projects/demo --defect task.spawner:ac.spawner_tests:9   # tiêm lỗi để xem repair loop
shams review projects/demo
shams decide projects/demo task.spawner retry
shams run projects/demo
shams events projects/demo --task task.spawner
```

## Gắn model AI và mở rộng

```bash
shams providers                        # các provider có sẵn
shams init projects/game --project-id my_game --goal "..." --domain game --opt engine=godot
cp projects/game/nodes.example.toml projects/game/nodes.toml   # khai báo model / API của bạn
shams nodes projects/game --check      # kiểm tra kết nối từng worker
```

Ví dụ `nodes.toml` (đầy đủ ở [`shams/examples/nodes.example.toml`](shams/examples/nodes.example.toml)):

```toml
[[node]]
id = "node.local"
kind = "local"
gpus = [{ index = 0, vram_mb = 12288 }]

[[worker]]
id = "worker.coder"
provider = "ollama"                    # llamacpp | lmstudio | vllm | openai_compatible | openai | anthropic | gemini | ...
model = "qwen2.5-coder:14b"
node = "node.local"
lane = "gpu"
vram_mb = 10000
capabilities = ["code.godot_gdscript", "integration"]
```

- **API key** không ghi trong file: dùng `config = { api_key_env = "TEN_BIEN_MOI_TRUONG" }`.
- **Công cụ sinh ảnh/3D/âm thanh**: `provider = "command"`, `config = { argv = [...] }`. Chương trình đọc `$SHAMS_CONTEXT` và ghi output vào các đường dẫn được giao.
- **Code riêng**: `config = { impl = "goi_cua_ban.module:Class" }` cho worker; `provider = "goi.module:Class"` cho provider.
- **Lĩnh vực mới**: viết một lớp con của `shams.packs.DomainPack` (options, template, domain_context, failure_rules, checkers…), rồi `register_pack()` hoặc khai báo entry point `shams.packs`. Xem [`shams_packs/software`](shams_packs/software/__init__.py) làm mẫu.
- **Engine game mới**: thêm `engines/<ten>.toml` (mẫu: [`shams_packs/game/engine.example.toml`](shams_packs/game/engine.example.toml)).
- **Template riêng**: đặt vào thư mục `templates/` của project.
- **Checker thật**: `shams.verification.qc.register_checker("compile", fn)`.
- **Đóng gói plugin**: khai báo entry point `shams.packs`, `shams.providers`, `shams.workers`, `shams.engines`, `shams.checkers` hoặc `shams.templates` trong gói Python của bạn.

## Ứng dụng

```bash
pip install -e ".[ui]"
# Ứng dụng desktop (khuyên dùng, xem docs/DESKTOP.md):
scripts\desktop-dev.ps1          # cửa sổ SHAMS (Tauri); scripts\desktop-build.ps1 tạo installer .exe/.msi
# Bản web cũ (dự phòng):
shams ui                        # workspace mặc định ~/SHAMS, tự mở http://127.0.0.1:8765
shams ui D:\SHAMS_Projects      # hoặc thư mục bạn chọn
```

- Gõ yêu cầu vào Chat là SHAMS tạo dự án, nhận lĩnh vực và stack/engine ("API bằng TypeScript", "game bằng UE5").
- **Dự án mới** và danh sách dự án ở cột trái.
- **Settings** (giống Claude Code), lưu vào phạm vi Người dùng / Dự án / Máy này:
  - Chung: ngôn ngữ, chế độ vận hành, lĩnh vực mặc định, chế độ thử, số vòng gọi tool.
  - Quyền: Mặc định / Toàn quyền / Chỉ đọc, danh sách cho phép và cấm (`mcp__server__tool`, glob).
  - Model & API: thêm kết nối (Ollama, llama.cpp, LM Studio, vLLM, OpenAI, Anthropic, Gemini, OpenRouter…), kiểm tra, liệt kê model, thêm worker theo capability.
  - MCP: thêm server stdio hoặc HTTP, xem tool, cho phép từng tool.
  - Hooks, Phần cứng (đọc thật), Biến môi trường (đặt khóa cho phiên, không ghi xuống đĩa), Tệp cấu hình (sửa JSON trực tiếp).
- Panel bên phải hiển thị VRAM, tải GPU và RAM **thật** của máy.

Mẫu đầy đủ: [`shams/examples/settings.example.json`](shams/examples/settings.example.json). Dự án có `.mcp.json` (định dạng Claude Code) cũng được đọc.

```bash
shams hardware                  # GPU / VRAM / RAM / CPU thật
shams settings <dự án>          # settings đã gộp + kiểm tra lỗi + biến môi trường thiếu
shams mcp <dự án>               # kết nối MCP server, liệt kê tool và quyền
```

## Cấu trúc

```text
shams/            Lõi (không biết lĩnh vực; test bắt buộc)
  packs.py        DomainPack + registry (ADR-117)
  media.py        Media type chung; pack đăng ký thêm
  contracts/      JSON Schema v1 + validate()
  statemachine.py Bảng chuyển trạng thái + quyền theo actor
  events/         Event Store + projections
  artifacts/      Artifact Registry, Impact Analyzer
  nodes/          Compute Node, lanes, Capability Registry, mock registry
  scheduling/     Scheduler, Model Lifecycle (adapter theo backend)
  context/        Context Compiler
  orchestration/  Task Runner Graph (LangGraph), facade Shams (project.py)
  verification/   QC Runner, Failure Classifier, Repair Router
  planning/       Plan Validator, tìm và dựng template
  providers/      Provider cho model local / API (ADR-116)
  workers/        LLMWorker, CommandWorker, plugin worker
  plugins.py      Nạp plugin (entry points, 'module:Class')
  hardware.py     GPU/VRAM/RAM/CPU thật (ADR-118)
  settings.py     Settings nhiều lớp, connections, bí mật qua biến môi trường
  mcp.py          MCP client (stdio, HTTP) + quyền dùng tool
  hooks.py        Hooks theo sự kiện
  examples/       nodes.example.toml, settings.example.json
  mock/           Mock Worker + mock checkers
shams_packs/
  software/       Software Pack: stack Python, TypeScript; template software_basic
  game/           Game Pack: engine Unity, Unreal, Godot, Web + engine tùy biến; template vertical_slice_3d
apps/cli/         `shams` CLI
apps/api/         FastAPI: state, chat, projects, settings, connections, mcp, hardware
apps/ui/          Giao diện chính (một file HTML, không cần build)
docs/design/      Bộ thiết kế v1.1 + ADR-117
```

## Bước tiếp theo

1. V2.1: Intent & Scope Manager, Reuse Before Generate, Fidelity Tier, Generation Budget.
2. V2.2: checker thật cho Software Pack; Spike S2: checker engine game thật.
3. Phase 1b: Planner LLM tách nhiệm vụ theo từng yêu cầu, Deep Agents worker, chạy Spike S1/S3 với model thật.
