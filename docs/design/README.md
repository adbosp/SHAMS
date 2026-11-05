# SHAMS — Bộ thiết kế chi tiết v1.1 (Phase 0a)

**SHAMS = Sequential Heterogeneous AI Manufacturing System** · 2026-11-05

Bộ tài liệu này hiện thực hóa Phase 0a: tối ưu kiến trúc V1 trong `AI_Game_Studio_Chat_Export/` và đặc tả đủ chi tiết để bắt đầu code Phase 1. **Không đổi định hướng**: stack, nguyên tắc, lộ trình 4 phase và MVP cá mập Unity giữ nguyên. Theo yêu cầu của người dùng (2026-11-05), controller không còn gắn với RTX 3060 (ADR-114); cấu hình đó chỉ còn là hồ sơ tham chiếu. Chưa có code ứng dụng; chỉ có tài liệu và JSON Schema.

| File | Nội dung |
|---|---|
| `01_ADR.md` | 13 quyết định gốc + 14 tối ưu v1.1 + 6 câu hỏi mở |
| `02_State_Machine.md` | State machine task/artifact thống nhất, bảng chuyển trạng thái, bất biến |
| `03_Contracts.md` | Mô tả 11 schema + artifact contracts cho MVP |
| `04_Scheduling_and_Policies.md` | Lanes GPU/CPU, affinity scheduling, unload adapter, QC 5 bậc, retry/escalation, budget, quyền |
| `05_Component_Design.md` | Sơ đồ thành phần, interface, bảng SQLite, cấu trúc repo, luồng mẫu |
| `06_Benchmark_and_Spikes.md` | Spike S1–S3, benchmark B01–B10, điều kiện vào Phase 1 |
| `schemas/` | JSON Schema draft 2020-12 (đã kiểm tra hợp lệ) |
| `examples/` | Task, handoff mẫu cho `task.shark_movement` và node controller không GPU (validate PASS) |

## Các tối ưu chính so với v1-proposal

| # | Tối ưu | Lợi ích |
|---|---|---|
| ADR-101 | Một state machine; `QC_PENDING` là trạng thái task, `READY_FOR_QC` là lời khai trong handoff; thêm `PENDING`, `LEASED`, `STALE`, `FAILED` | Hết mâu thuẫn; có chỗ cho invalidation |
| ADR-102 | Scheduler Python deterministic + LangGraph Task Runner Graph cho từng attempt | Crash cô lập theo task; graph vẫn cố định; dễ resume |
| ADR-103 | Model-affinity scheduling chủ động, có chống đói | Ít lần load/unload hơn keep-warm thụ động |
| ADR-104 | Event log là nguồn sự thật duy nhất | Không lệch trạng thái giữa DB, checkpoint, Git sau crash |
| ADR-105 | Artifact store theo hash; Git chỉ cho code/text; worktree theo attempt | Repo gọn; rollback sạch |
| ADR-106 | QC rẻ trước đắt sau, dừng sớm | Không nạp model review khi compile đã fail |
| ADR-107 | Phân loại lỗi bằng quy tắc trước, Analysis LLM sau | Bớt một lần đổi model GPU cho phần lớn lỗi |
| ADR-108 | DAG template + LLM điền tham số | Kế hoạch ổn định hơn, ít token |
| ADR-109 | Unity/Blender có đồ họa cũng phải lấy lease GPU; batchmode chạy làn CPU | Tránh OOM; tận dụng CPU song song |
| ADR-110 | Invalidation theo hash, QC lại trước khi làm lại | Tránh rework hàng loạt |
| ADR-111 | Side-effect ledger | Resume không gọi lại API trả phí |
| ADR-112 | Human review qua CLI/API từ Phase 1 | Không phải đợi Tauri UI |
| ADR-113 | 3 chế độ, mặc định `guided` | Rõ điểm duyệt |
| ADR-114 | Controller chạy trên PC bất kỳ; model gắn vào qua Compute Node (local, LAN, API); lane sinh theo node; có Mock Worker | Không phụ thuộc RTX 3060; xây và kiểm thử controller trước khi có model |

## Bước tiếp theo

1. ~~Người dùng duyệt bộ thiết kế này~~ — đã duyệt 2026-11-05.
2. Phase 1a: spike S0, S2 (PC bất kỳ), rồi xây controller và nghiệm thu bằng Mock Worker (`06 §5`).
3. Phase 1b: gắn Compute Node có model đầu tiên, chạy S1, S3, rồi coding worker thật.

Ghi chú: hai file nguồn trong `AI_Game_Studio_Chat_Export/` vẫn ghi tên cũ "Sequential Heterogeneous AI Studio"; tên chính thức là tên ở đầu file này.
