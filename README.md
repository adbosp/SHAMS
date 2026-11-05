# SHAMS — Sequential Heterogeneous AI Manufacturing System

Hệ thống sản xuất bằng AI dùng chung: một **bộ điều khiển** chia việc theo Task DAG, giao từng việc cho model AI chuyên môn phù hợp (code, reasoning, image, 3D, animation, audio), kiểm thử độc lập và sửa lỗi theo vòng lặp có giới hạn để tạo ra game hoàn chỉnh. Không gắn với một game, một model hay một nhà cung cấp: **mọi model local, mọi API, mọi engine đều gắn thêm được** (ADR-116).

Thiết kế chi tiết: [`docs/design/`](docs/design/README.md) (ADR, state machine, contracts, scheduling, component design, benchmark).

## Trạng thái: Phase 1a — Controller

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
| Engine / template / checker tùy biến (TOML hoặc entry point) | ✅ |
| Deep Agents worker (tool-call nhiều bước), Analysis Agent LLM, Planner LLM | ⏳ Phase 1b |
| Đa engine: Unity, Unreal Engine 5, Godot, Three.js/WebGPU (ADR-115), template trung lập engine | ✅ (mock) |
| Checker engine thật (compile/test/build/screenshot cho 4 engine; lệnh đã dựng sẵn) | ⏳ Spike S2 |
| Git worktree theo attempt (ADR-105) | ⏳ |
| Giao diện chính (`shams ui`): Chat, Workflow, AI Workers, Models, Artifacts, QA, Console | ✅ |
| SSE thay polling, Node Agent cho máy remote, desktop Tauri | ⏳ Phase 2/4 |

## Chạy thử (mock, không cần GPU)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q

shams engines                                     # unity | unreal (ue5) | godot | web (three.js/WebGPU)
shams init projects/demo --project-id demo_game --goal "Game đua xe 3D" --engine godot --mock
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
shams init projects/game --project-id my_game --goal "..." --engine godot
cp projects/game/nodes.example.toml projects/game/nodes.toml   # khai báo model / API của bạn
shams nodes projects/game --check      # kiểm tra kết nối từng worker
```

Ví dụ `nodes.toml` (đầy đủ ở [`studio/examples/nodes.example.toml`](studio/examples/nodes.example.toml)):

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
- **Engine mới**: thêm `engines/<ten>.toml` (mẫu: [`studio/examples/engine.example.toml`](studio/examples/engine.example.toml)).
- **Template riêng**: đặt vào thư mục `templates/` của project.
- **Checker thật**: `studio.verification.qc.register_checker("compile", fn)`.
- **Đóng gói plugin**: khai báo entry point `shams.providers`, `shams.workers`, `shams.engines`, `shams.checkers` hoặc `shams.templates` trong gói Python của bạn.

## Giao diện chính

```bash
pip install -e ".[ui]"
shams ui projects/demo          # mở http://127.0.0.1:8765, chọn engine hoặc ghi trong câu chat ("... bằng UE5")
```

Mở trực tiếp `apps/ui/index.html` (không chạy server) sẽ vào **chế độ demo** với dữ liệu mô phỏng.

## Cấu trúc

```text
studio/
  contracts/      JSON Schema v1 + validate()
  statemachine.py Bảng chuyển trạng thái + quyền theo actor
  events/         Event Store + projections
  artifacts/      Artifact Registry, Impact Analyzer
  nodes/          Compute Node, lanes, Capability Registry, mock registry
  scheduling/     Scheduler, Model Lifecycle (adapter theo backend)
  context/        Context Compiler
  orchestration/  Task Runner Graph (LangGraph), Studio facade
  verification/   QC Runner, Failure Classifier, Repair Router
  planning/       Plan Validator, DAG templates
  engines/        Engine Profile + Engine Adapter (Unity, Unreal, Godot, Web, engine tùy biến)
  providers/      Provider cho model local / API (ADR-116)
  workers/        LLMWorker, CommandWorker, plugin worker
  plugins.py      Nạp plugin (entry points, 'module:Class')
  examples/       nodes.example.toml, engine.example.toml
  mock/           Mock Worker + mock checkers
  templates/      vertical_slice_3d.json (khung vertical slice 3D chung, trung lập engine)
apps/cli/         `shams` CLI
apps/api/         FastAPI: /api/state, /api/chat, /api/decide
apps/ui/          Giao diện chính (một file HTML, không cần build)
docs/design/      Bộ thiết kế v1.1
```

## Bước tiếp theo

1. Spike S2: checker engine thật (bắt đầu với engine bạn dùng trước).
2. Phase 1b: Planner LLM tách nhiệm vụ theo từng yêu cầu, Deep Agents worker, chạy Spike S1/S3 với model thật.
