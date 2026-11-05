# SHAMS — Thiết kế thành phần

## 1. Sơ đồ tổng thể (v1.1)

```text
Desktop UI (Phase 4) / CLI `shams` / FastAPI
        │  REST + SSE (event stream)
        ▼
┌──────────────────────────── Studio Core (1 process, ghi DB duy nhất) ────────────────────────────┐
│  Plan Service ──► Planning Graph (LangGraph) ──► Plan Validator ──► task.created events           │
│                                                                                                   │
│  Studio Scheduler (Python deterministic)                                                          │
│     ├─ Lane manager: gpu(1) · cpu(3) · external(budget)                                           │
│     ├─ Affinity scoring + anti-starvation                                                         │
│     └─ Lease manager (TTL, thu hồi)                                                               │
│            │ khởi chạy                                                                            │
│            ▼                                                                                      │
│  Task Runner Graph (LangGraph, 1 thread_id / attempt)                                             │
│     prepare_context → acquire → ensure_loaded → execute → persist → keep_warm? → release → qc     │
│            │                   │                     │                                            │
│            │          Model Lifecycle Manager    Worker Adapters                                  │
│            │          (adapter/backend + NVML)   ├─ LLM Agent (Deep Agents + Ollama/llama.cpp)    │
│            │                                     ├─ Inference (ComfyUI, 3D, audio, motion)        │
│            │                                     └─ Tool (Unity CLI, Blender, Git, validators)    │
│            ▼                                                                                      │
│  QC Runner (5 bậc) ──► FailureClassifier ──► Repair Router ──► Repair Graph (Analysis Agent)      │
│                                                                                                   │
│  Context Compiler · Capability Router · Dependency Impact Analyzer · Side-effect Ledger           │
│  Node Registry (nodes.toml + health check) · Mock Worker (chạy khi chưa gắn model)                │
│                                                                                                   │
│  Event Store (SQLite, append-only) ─► projections: tasks, artifacts, leases, models               │
│  Artifact Store (content-addressed) · Git workspace (branch/worktree theo attempt)                │
│  LangGraph SqliteSaver (tiến độ trong attempt)                                                    │
└───────────────────────────────────────────────────────────────────────────────────────────────────┘
        │ HTTP (provider API hoặc Node Agent)            ▲ artifact upload/download theo hash
        ▼                                                │
  Compute Nodes (gắn sau, tùy chọn — ADR-114)
  ├─ node.controller  : chính PC chạy controller (CPU, có/không GPU, Unity/Blender nếu cài)
  ├─ node.<gpu_box>   : máy LAN bất kỳ có GPU chạy Ollama/llama.cpp/ComfyUI/Unity
  └─ node.<external>  : API bên thứ ba (budget, rate limit)
```

**Studio Core chỉ cần CPU và chạy trên PC bất kỳ.** Không có node GPU nào thì hệ thống vẫn chạy đủ vòng đời bằng Mock Worker và tool CPU (Unity batchmode, Git, validator).

## 2. Trách nhiệm và interface (Python, chữ ký tham khảo)

| Thành phần | Trách nhiệm | Interface chính | Dựa trên |
|---|---|---|---|
| Event Store | Ghi event + cập nhật projection trong 1 transaction | `append(event) -> seq`, `stream(since_seq)` | SQLite (WAL) |
| Artifact Registry | Lưu file theo hash, quản lý version/state | `put(path, logical_id, contract, produced_by) -> ArtifactRef`, `get(sha256)`, `accept(sha256)` | filesystem + SQLite |
| Plan Service | Chọn template, gọi Planning Graph, validate | `create_plan(goal, mode) -> TaskDAG` | LangGraph + Deep Agents |
| Plan Validator | Chu trình, producer cho mọi input, capability tồn tại, acceptance criteria có checker | `validate(dag) -> list[Issue]` | networkx hoặc thuật toán topo tự viết ngắn |
| Studio Scheduler | Chọn task, cấp lease, chống đói | `tick()`, `on_event(e)` | Python thuần |
| Node Registry | Đọc `nodes.toml`, health check, sinh lane, theo dõi node mất kết nối | `nodes()`, `lanes()`, `health(node_id)` | HTTP ping, provider API |
| Model Lifecycle Manager | Load/unload, xác nhận VRAM **theo node/GPU** | `ensure_loaded(worker, node, gpu)`, `unload(node, gpu)`, `vram_free_mb(node, gpu)` | Ollama API, process control, `pynvml` (local) / Node Agent (remote) |
| Mock Worker | Trả artifact mẫu + handoff hợp lệ theo contract, có thể cấu hình để FAIL | `run(context_package) -> Handoff` | Python thuần |
| Capability Router | Chọn worker cho capability | `select(capability, constraints) -> WorkerRef` | Registry + điểm chất lượng |
| Context Compiler | Tạo Context Package trong token budget | `compile(task, attempt) -> ContextPackage` | Deterministic; chỉ dùng LLM tóm tắt khi summary vượt giới hạn |
| LLM Agent Adapter | Chạy Deep Agents với tools giới hạn, ép trả handoff | `run(context_package) -> Handoff` | Deep Agents (structured output) |
| Inference Adapter | Gọi ComfyUI/3D/audio với tham số có cấu trúc | `run(context_package) -> Handoff` | ComfyUI API, process |
| Tool Adapter | Unity CLI, Blender headless, Git | `compile()`, `run_tests(filter)`, `build()`, `capture(scene)` | subprocess |
| QC Runner | Chạy 5 bậc, tạo QC report + failure record | `run(task, handoff, gate) -> QCReport` | Tool adapters + model review |
| FailureClassifier | Gán `failure_class`, `signature` | `classify(qc_report, logs) -> FailureRecord` | Quy tắc regex |
| Repair Router | Áp dụng thang leo thang `04 §3.2` | `route(failure) -> RepairPlan` | Python + Repair Graph khi cần |
| Dependency Impact Analyzer | Đánh dấu `STALE` theo hash | `on_artifact_accepted(ref)` | Đồ thị input hash |
| Side-effect Ledger | Idempotency | `guard(key, fn)` | SQLite |

## 3. Bảng SQLite (V1)

```
events(seq PK, event_id UNIQUE, project_id, type, subject_kind, subject_id, actor_json,
       from_state, to_state, payload_json, idempotency_key, ts)
projects(project_id PK, goal, mode, engine, engine_version, created_at)
plans(plan_id, project_id, plan_version, template, dag_json, approved_by, created_at)
tasks(task_id PK, project_id, plan_id, state, capability, priority, attempt, max_attempts,
      task_json, updated_seq)
task_deps(task_id, depends_on_task_id)
artifacts(sha256, logical_id, version, project_id, state, media_type, contract, storage,
          store_path, git_commit, produced_task_id, produced_attempt, metadata_json, license,
          created_at, PRIMARY KEY(sha256, project_id))
handoffs(task_id, attempt, status, handoff_json, ts, PRIMARY KEY(task_id, attempt))
handoff_inputs(task_id, attempt, sha256)          -- phục vụ invalidation
qc_reports(task_id, attempt, gate, verdict, stopped_at_stage, report_json, ts)
failures(failure_id PK, task_id, attempt, failure_class, signature, severity, record_json, ts)
nodes(node_id PK, kind, endpoint, node_json, enabled, health, checked_at)
leases(lease_id PK, task_id, attempt, node_id, lane, worker_id, granted_at, expires_at, released_at)
models(worker_id, node_id, gpu_index, provider, model, model_version, loaded, last_loaded_at,
       last_unloaded_at, PRIMARY KEY(worker_id, node_id, gpu_index))
capabilities(worker_id PK, entry_json, enabled)
side_effects(idempotency_key PK, task_id, attempt, action, status, result_json, ts)
metrics(task_id, attempt, key, value, ts)         -- tokens, vram, latency, cost
```

## 4. Cấu trúc repository (cập nhật từ tài liệu gốc §16)

```text
SHAMS/
├── apps/
│   ├── desktop/              # Tauri + React (Phase 4)
│   ├── api/                  # FastAPI + SSE
│   └── cli/                  # `shams` CLI: run, status, review, approve (Phase 1)
├── studio/
│   ├── contracts/            # JSON Schema + model Pydantic sinh từ schema
│   ├── events/               # Event Store, projections
│   ├── orchestration/        # Planning / Task Runner / Repair graphs (LangGraph)
│   ├── scheduling/           # Scheduler, lanes, leases, affinity
│   ├── nodes/                # Node Registry, lanes, health; node_agent/ (Phase 2+, chạy trên máy GPU)
│   ├── lifecycle/            # Model Lifecycle Manager, NVML
│   ├── mock/                 # Mock Worker cho chế độ chưa gắn model
│   ├── agents/               # Deep Agents wrappers, tool allowlists
│   ├── capabilities/         # Registry, Router
│   ├── context/              # Context Compiler
│   ├── artifacts/            # Artifact Registry, Impact Analyzer
│   ├── verification/         # QC Runner, Classifier, Repair Router
│   ├── templates/            # DAG templates (unity_3d_vertical_slice)
│   └── security/             # Path allowlist, shell allowlist, side-effect ledger
├── providers/                # ollama, llamacpp, comfyui, model_3d, audio, external_api
├── engines/
│   ├── unity/                # CLI wrapper + Editor scripts (build, test, capture, validate)
│   └── unreal/               # Phase 4
├── tools/blender/            # Script validate/fix mesh headless
├── benchmarks/               # Task set B-set, runner, báo cáo
├── projects/                 # Workspace dự án game (ngoài Git của SHAMS)
├── docs/design/              # Bộ tài liệu này
└── pyproject.toml            # pin langgraph, deepagents, langchain-ollama
```

## 5. Luồng một task điển hình (code Unity)

1. Scheduler chọn `task.shark_movement` (READY, affinity với model code đang nạp trên `node.gpu_box` GPU 0) → `lease.granted(gpu:node.gpu_box:0)`. Nếu chưa có node phục vụ `code.unity_csharp`: chạy Mock Worker (mock mode) hoặc `BLOCKED`.
2. Task Runner: Context Compiler tạo package (scaffold summary, file liên quan dạng signature, Unity version, workspace scope).
3. `ensure_loaded(worker.code_local)` → model đã nạp, bỏ qua load.
4. Deep Agents chạy trên Git worktree `task/shark_movement/a0`, chỉ ghi `Assets/Scripts/Shark/**`, `Assets/Tests/Shark/**`; được gọi tool `unity_compile` làm self-check.
5. Kết thúc bằng structured output = handoff → Artifact Registry `put` các file → handoff persist → `task.submitted`.
6. `decide_keep_warm`: còn `task.prey_eat` cùng model sắp READY → giữ model; trả lease GPU.
7. QC bậc 1–3 chạy làn CPU (compile + test batchmode) song song khi GPU phục vụ task khác. Bậc 4 (screenshot, tiêu chí không bắt buộc) xếp hàng lease GPU.
8. PASS → `qc.passed` → artifact `ACCEPTED` → merge branch → `task.prey_eat` READY.
