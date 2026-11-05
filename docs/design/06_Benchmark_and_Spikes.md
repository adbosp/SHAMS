# SHAMS — Benchmark B-set và Spike Phase 0b

## 1. Spike Phase 0b

Theo ADR-114, controller không phụ thuộc phần cứng, nên spike chia làm hai nhóm. Spike là code thử nghiệm có giới hạn thời gian, **bỏ đi sau khi đo**; chỉ giữ lại số liệu và kết luận.

**Nhóm A — chạy trên PC bất kỳ, không cần GPU, không cần model (làm ngay):**

| ID | Câu hỏi | Cách làm | Tiêu chí kết luận | Quyết định ảnh hưởng |
|---|---|---|---|---|
| S0 | LangGraph + SQLite có đáp ứng vòng đời task, interrupt, crash/resume không? | Task Runner Graph với Mock Worker; kill process giữa chừng rồi resume; thử `interrupt()` cho human review | Resume đúng node, không lặp side effect, state machine đúng bảng `02` | ADR-001, ADR-102, ADR-104 |
| S2 | Unity CLI tự động hóa được đến đâu? | Batchmode: tạo project từ template, compile, EditMode + PlayMode test, build Windows (chạy trên CPU). Chế độ có đồ họa: mở scene, chụp screenshot (cần GPU tích hợp hoặc rời bất kỳ) | Mọi bước chạy không cần tương tác | ADR-109 |

**Nhóm B — chạy khi gắn Compute Node có model đầu tiên (bất kỳ GPU nào, hoặc API):**

| ID | Câu hỏi | Cách làm | Tiêu chí kết luận | Quyết định ảnh hưởng |
|---|---|---|---|---|
| S1 | Model local nào gọi tool đủ ổn định với Deep Agents? | 2–3 model code/reasoning vừa VRAM của node đó (chọn bản mới nhất có license phù hợp tại thời điểm chạy) × 5 task B-set dễ, mỗi cặp 3 lần | Tool-call hợp lệ ≥ 80%; ≥ 3/5 task đạt acceptance | ADR-002; OPEN-1 cho hồ sơ phần cứng đó |
| S3 | Chi phí chuyển model là bao nhiêu? | Đo load/unload p50/p95 cho Ollama (2 model), ComfyUI (1 checkpoint), xác minh cách giải phóng VRAM của ComfyUI; đo VRAM sót lại | Có bảng số; unload xác nhận được bằng NVML | ADR-103 trọng số, `04 §1.4` |

S3 cũng đo VRAM của Unity có đồ họa khi chạy cạnh model trên cùng node, để quyết định `gpu_shared_mode` cho node đó. Kết quả ghi thành `spikes/S*.md` + cập nhật ADR. Mỗi node mới có phần cứng khác chạy lại S1/S3 để có số riêng.

## 2. Benchmark B-set (Unity, dùng từ Phase 1 về sau)

Mỗi task có project khởi đầu cố định (commit hash), acceptance criteria tự động, và thời gian tối đa.

| ID | Độ khó | Task | Acceptance tự động |
|---|---|---|---|
| B01 | Dễ | Thêm script xoay object theo trục với tốc độ cấu hình | Compile + EditMode test góc sau N frame |
| B02 | Dễ | Sửa lỗi compile được cài sẵn (3 lỗi) | Compile |
| B03 | Dễ | HUD Text hiển thị điểm, tăng khi gọi `AddScore` | Compile + PlayMode test giá trị text |
| B04 | Trung bình | Điều khiển nhân vật bơi 3D bằng Rigidbody (WASD + chuột) | PlayMode test vị trí/hướng sau input giả lập |
| B05 | Trung bình | Spawner cá mồi trong vùng, giới hạn số lượng | PlayMode test đếm, vị trí trong bounds |
| B06 | Trung bình | Cơ chế ăn: va chạm miệng–mồi, mồi biến mất, size tăng | PlayMode test |
| B07 | Trung bình | Sửa bug logic được cài sẵn có test đang fail | Test chuyển xanh, không test khác đỏ |
| B08 | Khó | Refactor: tách input khỏi movement qua interface, giữ test cũ xanh | Toàn bộ test xanh + kiểm tra cấu trúc |
| B09 | Khó | Tích hợp B04–B06 thành vòng chơi + điều kiện thua | Integration PlayMode test + build Windows chạy 30 s không exception |
| B10 | Khó | Build Windows + smoke test tự động | `.exe` chạy, log sạch |

## 3. Cấu hình so sánh (giữ từ tài liệu gốc §18)

- **A:** model local một mình (một lần gọi, không tool).
- **B:** model local + harness (Deep Agents, tool Unity).
- **C:** SHAMS đầy đủ (scheduler + handoff + QC + repair).
- **D:** Codex hoặc Claude Code với tool tương đương (khi đảm bảo điều kiện công bằng).

## 4. Chỉ số ghi cho mỗi lần chạy

`accepted` (bool), `attempts`, `repairs`, `human_interventions`, `wall_seconds`, `tokens_in/out`, `cost_usd`, `peak_vram_mb`, `peak_ram_mb`, `model_switches`, `regressions`, `time_to_first_playable_build`.

Kết quả lưu `benchmarks/results/<date>/<config>/<task>.json`; điểm chất lượng ghi ngược vào `capability.quality` để router dùng.

## 5. Điều kiện vào Phase 1

Phase 1 tách làm hai bước để không bị chặn bởi phần cứng:

**Phase 1a — Controller (PC bất kỳ, chưa gắn model)**
- [x] Người dùng duyệt bộ thiết kế v1.1 (2026-11-05).
- [ ] S0 và S2 có kết luận.
- [ ] Xây Studio Core: Event Store, Artifact Registry, Node Registry, Scheduler, Task Runner Graph, QC bậc 1–3, Repair Router, CLI.
- [ ] Nghiệm thu bằng Mock Worker: chạy hết DAG mẫu cá mập, QC FAIL giả lập → repair → PASS, crash/resume, invalidation STALE.
- [ ] B-set B01–B07 có project khởi đầu và test tự động.

**Phase 1b — Gắn model đầu tiên**
- [ ] Khai báo Compute Node đầu tiên (GPU bất kỳ hoặc API).
- [ ] S1 và S3 có kết luận cho node đó; chọn model code mặc định (OPEN-1).
- [ ] Coding worker thật sửa/tạo feature Unity nhỏ và phục hồi từ checkpoint (tiêu chí PASS Phase 1 gốc).
