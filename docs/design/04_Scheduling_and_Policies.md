# SHAMS — Scheduling và chính sách vận hành

Mọi con số dưới đây là **giá trị mặc định V1**, cấu hình trong `studio.toml`. Số đánh dấu ⚑ phải hiệu chỉnh bằng Spike S3 **trên node thực tế** khi gắn model.

## 1. Studio Scheduler (ADR-102, ADR-103, ADR-109, ADR-114)

### 1.0 Compute Node

Controller chạy trên PC bất kỳ, không cần GPU. Tài nguyên được khai báo theo node trong `nodes.toml` (schema `node.schema.json`):

```toml
[[node]]                       # chính máy controller, không GPU
id = "node.controller"
kind = "local"
cpu_slots = 3
engines = ["unity:6000.0"]     # nếu có cài

[[node]]                       # ví dụ một máy LAN có GPU (gắn sau)
id = "node.gpu_box"
kind = "remote"
endpoint = "http://192.168.1.20"
gpus = [{ index = 0, vram_mb = 12288 }]
providers = ["ollama", "comfyui"]

[[node]]                       # API bên ngoài
id = "node.external_x"
kind = "external"
budget_usd = 5
```

Khi chưa có node nào phục vụ một capability, task cần capability đó vào `BLOCKED` (`tool_unavailable`) hoặc chạy bằng worker `mock` nếu project bật `mock_mode`.

### 1.1 Làn tài nguyên (resource lanes)

Lane được **sinh tự động từ node**, không cố định.

| Lane | Sức chứa | Ví dụ |
|---|---|---|
| `gpu:<node>:<index>` | 1 lease độc quyền mỗi GPU (V1) | LLM Ollama, ComfyUI, model 3D/audio, Unity play mode/screenshot, Blender render trên node đó |
| `cpu:<node>` | `cpu_slots` của node | Unity `-batchmode -nographics`, Git, validator, Blender headless, Context Compiler |
| `external:<node>` | theo budget + rate limit | API bên ngoài (chỉ khi bật, ADR-008) |

Máy không GPU vẫn có lane CPU nên vẫn chạy được compile/test/build Unity và toàn bộ logic điều phối. Task cần GPU chỉ được xếp lên node có GPU đủ `vram_mb_est`. V1 **không** chia một GPU cho nhiều consumer; chỉ bật `gpu_shared_mode` cho một node khi Spike S2 đo được an toàn trên node đó.

Artifact giữa các node đi qua Artifact Store của controller (HTTP upload/download theo hash). Node không giữ trạng thái; mất kết nối node = lease hết hạn = task `REPAIR_REQUIRED` (`source=timeout`), không tính vào số attempt.

### 1.2 Vòng lặp scheduler (mỗi tick hoặc khi có event)

1. Thu hồi lease hết hạn (`lease_ttl = budget.max_wall_seconds × 1.5`), task tương ứng → `REPAIR_REQUIRED` (`source=timeout`).
2. Cập nhật `PENDING → READY` cho task đủ dependency.
3. Với mỗi lane còn chỗ, chọn task theo **điểm**:

```
score = priority
      + 30  nếu worker cần dùng đúng model đang nạp trên GPU của node được chọn (affinity theo node, gpu, model)
      + 15  nếu task nằm trên critical path của milestone hiện tại (vertical slice)
      + min(wait_minutes, 60)                      (chống đói)
      − switch_cost_seconds / 10   ⚑               (chi phí unload+load đo được)
```

4. Ràng buộc chống đói: không chạy quá `affinity_max_streak = 6` ⚑ task liên tiếp cùng model nếu có task khác READY chờ > `max_wait_minutes = 20`.
5. Cấp lease → event `lease.granted` → khởi chạy Task Runner Graph.

### 1.3 Model lifecycle trong Task Runner

```
acquire_lease → ensure_loaded(model) → execute → persist_artifacts → persist_handoff
   → decide_keep_warm → release_lease → qc
```

- `ensure_loaded`: nếu model khác đang nạp trên GPU đó → `unload` qua adapter của **backend đang giữ GPU** → đợi `vram_free ≥ vram_mb_est + safety_margin_mb (1024)` → `load`. VRAM đo bằng NVML trên node (node local: trực tiếp; node remote: qua Node Agent hoặc API của provider như Ollama `/api/ps`). Node `external` bỏ qua bước này.
- `decide_keep_warm`: giữ model nếu trong hàng đợi READY/PENDING sắp READY có task cùng model trong `keep_warm_horizon = 2` lượt chọn kế tiếp; ngược lại unload ngay.
- `persist_handoff` **bắt buộc thành công** trước `release_lease`; nếu không persist được → giữ output trong thư mục attempt, đánh dấu `invalid_handoff`, vẫn trả lease.

### 1.4 Unload adapter theo backend

| Backend | Load | Unload | Kiểm tra |
|---|---|---|---|
| Ollama | request đầu tiên với `keep_alive` | `keep_alive: 0` | `/api/ps` + NVML |
| llama.cpp server | khởi động process với model | dừng process | process exit + NVML |
| ComfyUI | workflow đầu tiên | API giải phóng bộ nhớ của ComfyUI ⚑ (xác minh trong S3), hoặc restart process | NVML |
| Model 3D/audio Python | process riêng | dừng process (cách chắc chắn nhất) | process exit + NVML |
| Unity/Blender có đồ họa | khởi động process | đóng process | process exit + NVML |

Nguyên tắc: backend không có API unload tin cậy thì **chạy trong process riêng và kill process**.

## 2. QC pipeline (ADR-106)

| Bậc | Kiểm tra | Chạy trên | Dừng nếu FAIL |
|---|---|---|---|
| 1 | Output tồn tại, hash khớp, handoff + artifact đúng schema, file nằm trong `workspace_scope` | cpu | ✓ |
| 2 | Compile Unity (batchmode), validator asset (Blender headless: poly, UV, scale, rig) | cpu | ✓ |
| 3 | Unit/EditMode/PlayMode test (batchmode), regression suite của các task `ACCEPTED` liên quan | cpu | ✓ |
| 4 | Chạy scene, log runtime, screenshot/video ngắn, metric (FPS) | **gpu lease** | ✓ |
| 5 | Model review semantic/visual — chỉ khi có tiêu chí `semantic`/`visual` | gpu lease | — |

- Checker ở bậc 5 phải là **model khác** với producer khi có thể; nếu cùng model, ghi rõ trong `qc_report.criteria[].checker`.
- Tiêu chí bắt buộc không có checker → `NOT_VERIFIED` → `HUMAN_REVIEW` (chế độ `collaborative`/`guided`) hoặc chấp nhận có cờ (chế độ `autonomous`, chỉ với tiêu chí không phải `compile`/`unit_test`).
- **Integration QC** chạy khi tất cả task của một milestone `ACCEPTED`. **Final QC** chạy trên build Windows.

## 3. Retry, repair và escalation (ADR-107)

### 3.1 Failure classifier (deterministic)

| Dấu hiệu | `failure_class` | Route mặc định |
|---|---|---|
| Unity log `error CS\d+` | `compile_error` | `fix_in_place` cùng worker code, kèm log lỗi |
| Test fail theo tên | `test_failure` | `fix_in_place` |
| Exception trong log runtime | `runtime_exception` | `fix_in_place`, kèm stack trace |
| Thiếu output / sai schema | `missing_output` / `schema_violation` | `retry_with_context` (nhắc output contract) |
| Validator mesh/rig fail | `asset_validation` | Blender tool tự sửa nếu có rule; nếu không, `switch_worker` |
| CUDA OOM | `resource_oom` | Scheduler giảm tham số / đổi model nhỏ hơn; không tính vào attempt |
| Tool không có | `tool_unavailable` | `BLOCKED` |
| Không khớp quy tắc | `unknown` | Analysis Agent |

`signature` = thông điệp lỗi đã bỏ số dòng, đường dẫn tuyệt đối, timestamp, rồi băm.

### 3.2 Thang leo thang

| Lần FAIL (cùng task) | Hành động |
|---|---|
| 1 | Theo route của classifier |
| 2 cùng `signature` | Analysis Agent tìm root cause; có thể `fix_upstream` (lỗi nằm ở task trước) |
| 3 | `split_task` hoặc `switch_worker` (model local khác có capability) |
| 4 | `escalate_external` nếu được bật và trong budget; nếu không, `HUMAN_REVIEW` |
| `max_attempts` (mặc định 3, tối đa 5) | Luôn `HUMAN_REVIEW` |

Giới hạn toàn cục: tổng repair task sinh ra cho một milestone ≤ `2 × số task ban đầu`; vượt → dừng milestone, `HUMAN_REVIEW`.

### 3.3 `fix_upstream` và invalidation (ADR-110)

Khi root cause nằm ở task đã `ACCEPTED`: tạo repair task cho task upstream → artifact mới → Dependency Impact Analyzer đánh dấu downstream `STALE` → các task `STALE` QC lại trước, chỉ rework nếu QC FAIL.

## 4. Budget và chi phí

| Mục | Mặc định |
|---|---|
| External API | Tắt. Khi bật: `max_cost_usd_per_project = 5`, `per_task = 1`; vượt → `HUMAN_REVIEW` |
| Token mỗi attempt LLM local | `max_tokens = 60000` (context + output) |
| Wall time mỗi task code | 20 phút; asset 3D 30 phút; build 15 phút |
| Context package | ≤ 60% context window của model được chọn; phần còn lại cho suy luận/tool output |

## 5. Quyền và an toàn

- Worker chỉ ghi trong `workspace_scope` của task; kiểm tra ở bậc QC 1 **và** chặn trước ở lớp tool (path allowlist trong Deep Agents backend).
- Lệnh shell: allowlist (Unity CLI, git status/diff/add/commit trên branch task, dotnet, blender headless); cấm `rm -rf` ngoài thư mục attempt, push, publish, network ngoài localhost.
- Hành động thuộc ADR-012 → `interrupt()` chờ `human.decided`.
- Mỗi side effect ngoài workspace đi qua side-effect ledger (ADR-111).

## 6. Chế độ vận hành (ADR-113)

| Điểm duyệt | collaborative | guided (mặc định) | autonomous |
|---|---|---|---|
| Duyệt Task DAG | ✓ | ✓ | |
| Duyệt mỗi milestone (Integration QC) | ✓ | | |
| `NOT_VERIFIED` | ✓ | ✓ | chỉ tiêu chí quan trọng |
| External API / chi phí | ✓ | ✓ | ✓ |
| Push / publish / xóa lớn | ✓ | ✓ | ✓ |
