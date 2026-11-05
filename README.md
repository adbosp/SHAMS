# SHAMS — Sequential Heterogeneous AI Manufacturing System

Nền tảng AI Studio độc lập: một **bộ điều khiển** phối hợp nhiều model AI chuyên môn (code, reasoning, image, 3D, animation, audio) theo Task DAG để tạo game hoàn chỉnh, có kiểm thử độc lập và vòng sửa lỗi có giới hạn.

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
| Worker thật (Deep Agents + Ollama/llama.cpp), Analysis Agent LLM | ⏳ Phase 1b |
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
shams init projects/shark --project-id shark_mvp --goal "Game cá mập 3D" --engine godot --mock
shams plan projects/shark --approve <tên-bạn>
shams run projects/shark --defect task.prey_spawner:ac.spawner_tests:9   # tiêm lỗi để xem repair loop
shams review projects/shark
shams decide projects/shark task.prey_spawner retry
shams run projects/shark
shams events projects/shark --task task.prey_spawner
```

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
  engines/        Engine Profile + Engine Adapter (Unity, Unreal, Godot, Web)
  mock/           Mock Worker + mock checkers
  templates/      vertical_slice_3d.json (MVP cá mập, trung lập engine)
apps/cli/         `shams` CLI
apps/api/         FastAPI: /api/state, /api/chat, /api/decide
apps/ui/          Giao diện chính (một file HTML, không cần build)
docs/design/      Bộ thiết kế v1.1
```

## Bước tiếp theo

1. Spike S2: checker engine thật (bắt đầu với engine bạn dùng trước).
2. Phase 1b: gắn Compute Node đầu tiên, worker Deep Agents + Ollama, chạy Spike S1/S3.
