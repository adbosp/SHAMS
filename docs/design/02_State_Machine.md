# SHAMS — State Machine thống nhất (ADR-101)

Mọi chuyển trạng thái là một **event** ghi vào `events` (ADR-104). Không thành phần nào được sửa trực tiếp cột `state`.

## 1. Task state

| State | Ý nghĩa | Terminal |
|---|---|---|
| `PENDING` | Đã tạo, còn dependency chưa `ACCEPTED` | |
| `READY` | Mọi dependency `ACCEPTED`, chờ scheduler | |
| `LEASED` | Scheduler đã cấp lease tài nguyên, đang chuẩn bị/nạp model | |
| `RUNNING` | Worker đang thực thi | |
| `QC_PENDING` | Worker đã nộp handoff `READY_FOR_QC`, chờ/đang QC | |
| `ACCEPTED` | QC PASS (hoặc PASS có điều kiện `NOT_VERIFIED` được người duyệt chấp nhận) | ✓ (có thể về `STALE`) |
| `REPAIR_REQUIRED` | QC FAIL, đã có Failure Record, chờ repair routing | |
| `STALE` | Từng `ACCEPTED` nhưng input upstream đổi hash | |
| `BLOCKED` | Thiếu thứ không tự giải quyết được (tool, quyền, file, capability) | |
| `HUMAN_REVIEW` | Chờ người: approval, vượt retry, quyết định thiết kế | |
| `FAILED` | Hết retry/escalation và người dùng chọn dừng | ✓ |
| `CANCELED` | Người dùng hoặc replan hủy | ✓ |

### Bảng chuyển trạng thái

| Từ | Đến | Event | Ai được phát |
|---|---|---|---|
| — | `PENDING` / `READY` | `task.created` | Planner (qua validator) |
| `PENDING` | `READY` | `task.unblocked` | Scheduler (khi dep cuối `ACCEPTED`) |
| `READY` | `LEASED` | `task.leased` | Scheduler |
| `READY` | `BLOCKED` | `task.blocked` | Scheduler (không node/worker nào cung cấp capability, ADR-114) |
| `LEASED` | `RUNNING` | `task.started` | Task Runner |
| `LEASED` | `READY` | `task.lease_released` | Scheduler (nạp model lỗi, ưu tiên khác) |
| `RUNNING` | `QC_PENDING` | `task.submitted` | Task Runner, chỉ khi handoff hợp lệ schema và đã persist |
| `RUNNING` | `BLOCKED` | `task.blocked` | Task Runner (handoff `status=BLOCKED`) |
| `RUNNING` | `REPAIR_REQUIRED` | `task.crashed` | Scheduler (timeout, crash, handoff không hợp lệ) |
| `QC_PENDING` | `ACCEPTED` | `qc.passed` | **Chỉ QC** |
| `QC_PENDING` | `REPAIR_REQUIRED` | `qc.failed` | **Chỉ QC** (kèm `failure_id`) |
| `QC_PENDING` | `HUMAN_REVIEW` | `qc.not_verified` | QC (thiếu tool kiểm chứng tiêu chí bắt buộc) |
| `REPAIR_REQUIRED` | `READY` | `repair.scheduled` | Repair Router (attempt + 1, kèm repair plan) |
| `REPAIR_REQUIRED` | `HUMAN_REVIEW` | `repair.exhausted` | Repair Router (vượt ngưỡng `04 §3`) |
| `ACCEPTED` | `STALE` | `task.invalidated` | Dependency Impact Analyzer |
| `STALE` | `QC_PENDING` | `task.revalidate` | Scheduler (QC lại trên input mới, ADR-110) |
| `BLOCKED` | `READY` | `task.unblocked` | Người dùng / Scheduler khi điều kiện được giải |
| `HUMAN_REVIEW` | `READY` / `ACCEPTED` / `FAILED` / `CANCELED` | `human.decided` | **Chỉ người dùng** |
| bất kỳ không terminal | `CANCELED` | `task.canceled` | Người dùng / Planner khi replan |

**Bất biến:**
1. `ACCEPTED` chỉ đến từ `qc.passed` hoặc `human.decided`.
2. Task chỉ `READY` khi mọi dependency `ACCEPTED` (không `STALE`).
3. Mỗi attempt có đúng một handoff và tối đa một QC report.
4. Lease luôn được trả trước khi task rời `LEASED`/`RUNNING` (Task Runner node `release` chạy cả trong nhánh lỗi; scheduler thu hồi lease hết hạn).

## 2. Artifact state

| State | Ý nghĩa |
|---|---|
| `CANDIDATE` | Worker vừa nộp, đã có hash, chưa QC |
| `ACCEPTED` | QC PASS, consumer được dùng |
| `REJECTED` | QC FAIL, giữ lại làm bằng chứng |
| `SUPERSEDED` | Đã có version mới `ACCEPTED` cho cùng `logical_id` |
| `STALE` | Được tạo từ input nay đã bị thay thế, chờ revalidate |

Một `logical_id` (ví dụ `asset.shark.mesh`) có nhiều version; tại mỗi thời điểm tối đa **một** version `ACCEPTED`. Consumer luôn tham chiếu bằng `sha256`, không bằng đường dẫn.

## 3. Handoff status (lời khai của worker)

`READY_FOR_QC` · `BLOCKED` · `PARTIAL` (làm được một phần, nêu rõ trong `known_issues`; vẫn đi QC) · `FAILED` (worker tự thấy không làm được → Scheduler chuyển `REPAIR_REQUIRED` không qua QC).

## 4. QC verdict

`PASS` · `FAIL` · `NOT_VERIFIED` (tiêu chí bắt buộc không có cách kiểm chứng tự động). Mỗi acceptance criterion có verdict riêng; verdict tổng = `FAIL` nếu bất kỳ tiêu chí bắt buộc FAIL, `NOT_VERIFIED` nếu không FAIL nhưng có tiêu chí bắt buộc chưa kiểm chứng, còn lại `PASS`.
