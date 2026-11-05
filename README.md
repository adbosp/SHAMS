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
| Checker Unity thật (batchmode compile/test/build, screenshot) | ⏳ Spike S2 |
| Git worktree theo attempt (ADR-105) | ⏳ |
| FastAPI + SSE, Node Agent cho máy remote | ⏳ Phase 2 |

## Chạy thử (mock, không cần GPU)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q

shams init projects/shark --project-id shark_mvp --goal "Game cá mập 3D" --mock
shams plan projects/shark --template unity_3d_vertical_slice --approve <tên-bạn>
shams run projects/shark --defect task.prey_spawner:ac.spawner_tests:9   # tiêm lỗi để xem repair loop
shams review projects/shark
shams decide projects/shark task.prey_spawner retry
shams run projects/shark
shams events projects/shark --task task.prey_spawner
```

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
  mock/           Mock Worker + mock checkers
  templates/      unity_3d_vertical_slice.json (MVP cá mập)
apps/cli/         `shams` CLI
docs/design/      Bộ thiết kế v1.1
```

## Bước tiếp theo

1. Spike S2: checker Unity thật qua CLI batchmode.
2. Phase 1b: gắn Compute Node đầu tiên, worker Deep Agents + Ollama, chạy Spike S1/S3.
