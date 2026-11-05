# SHAMS — Data Contracts v1

JSON Schema (draft 2020-12) nằm trong `schemas/`; ví dụ hợp lệ trong `examples/`. Mọi contract có `schema_version: "1.0"`. Đổi trường bắt buộc = tăng major version và viết migration.

| Schema | Ai tạo | Ai đọc | Ghi chú chính |
|---|---|---|---|
| `common` | — | tất cả | `id` dạng slug, `sha256`, `artifact_ref`, `worker_ref`, `budget`, `acceptance_criterion` |
| `task_dag` | Planner (template + LLM) | Plan Validator, người duyệt | Phải qua validator: không chu trình, mọi `inputs.logical_id` có producer, mọi capability có trong registry |
| `task` | Planner / Repair Router / người | Scheduler, Context Compiler | `acceptance_criteria` ≥ 1, `outputs` ≥ 1, `workspace_scope` giới hạn ghi |
| `context_package` | Context Compiler (deterministic) | Worker | Không chứa hội thoại cũ; `relevant_files[].inclusion` kiểm soát token |
| `handoff` | Worker (qua Task Runner) | QC, worker kế tiếp, Impact Analyzer | `READY_FOR_QC` cần ≥ 1 output; `BLOCKED` cần `blocked_reason`; `inputs` ghi đúng hash đã dùng |
| `artifact` | Artifact Registry | mọi thành phần | Bất biến theo hash; `state` theo `02 §2`; `license` ghi nguồn model tạo |
| `qc_report` | QC Runner | Scheduler, Repair Router, UI | Verdict từng tiêu chí + bậc dừng (ADR-106) |
| `failure_record` | QC Runner / Scheduler | Classifier, Analysis Agent, Repair Router | `signature` để phát hiện lỗi lặp; `repair_plan.strategy` theo `04 §3` |
| `capability` | Người cấu hình + Benchmark Lab | Capability Router, Scheduler | `runtime.node_id`, `runtime.lane`, `load_adapter`, VRAM, chất lượng đo được, license/privacy |
| `node` | Người cấu hình (`nodes.toml`) / Node Agent tự khai báo | Scheduler, Capability Router | Tài nguyên theo node: GPU/VRAM, CPU slots, provider, engine; controller không gắn phần cứng cụ thể (ADR-114) |
| `event` | Event Store | projection, UI, audit | Append-only, `seq` tăng dần, `idempotency_key` cho side effect |

## Artifact contracts (V1, MVP cá mập; Unity mặc định, engine khác ở bảng dưới)

`task.outputs[].contract` trỏ tới một contract có validator ở QC bậc 1–2.

| Contract | Định dạng | Validator V1 |
|---|---|---|
| `unity.csharp_script` | `.cs` trong `Assets/Scripts/**` | Compile batchmode; namespace theo dự án; không dùng API editor trong runtime script |
| `unity.test_script` | `.cs` trong `Assets/Tests/**` | Compile; test được Unity Test Runner phát hiện |
| `unity.scene` / `unity.prefab` | `.unity` / `.prefab` + `.meta` | Mở được bằng batchmode script; không missing script/reference |
| `image.texture` | PNG, cạnh lũy thừa 2, ≤ 2048 | Kích thước, kênh alpha theo yêu cầu |
| `image.ui_sprite` | PNG nền trong suốt | Kích thước, alpha |
| `mesh.fbx_game_ready` | FBX hoặc glTF/GLB, Y-up khi vào Unity, đơn vị mét | Blender headless: poly ≤ budget task (mặc định 20k tri cho nhân vật chính, 3k cho cá mồi), có UV, normals hợp lệ, scale ~ kích thước khai báo, không n-gon |
| `rig.humanoid` / `rig.generic` | FBX có armature | Số xương, tên xương theo mapping, skin weights tổng = 1 |
| `animation.clip` | FBX/anim | Độ dài, loop seamless (khoảng cách pose đầu-cuối ≤ ngưỡng), khớp rig |
| `audio.sfx` | WAV/OGG 44.1/48 kHz | Thời lượng, peak ≤ −1 dBFS, không im lặng hoàn toàn |
| `build.windows` | Thư mục build + `.exe` | Launch được, chạy smoke test N giây không crash, log không có exception |

### Contract theo engine (ADR-115)

| Engine | Script / test | Scene | Mesh | Build | Validator V1 (Spike S2) |
|---|---|---|---|---|---|
| Unity | `unity.csharp_script` (`Assets/Scripts`), `unity.test_script` (`Assets/Tests`) | `unity.scene` | `mesh.fbx_game_ready` | `build.windows` | `-batchmode -runTests`, build qua `-executeMethod` |
| Unreal Engine 5 | `unreal.cpp_module` (`Source/Game`), `unreal.automation_test` | `unreal.map` (`Content/Maps`) | `mesh.fbx_game_ready` (nhập qua Interchange) | `build.windows` (Win64) | `RunUAT BuildEditor`, `UnrealEditor-Cmd -nullrhi Automation RunTests`, `BuildCookRun` |
| Godot 4 | `godot.gdscript` (`scripts/`), `godot.gut_test` (`test/`) | `godot.scene` (`.tscn`) | `mesh.gltf_game_ready` | `build.windows` | `--headless --import`, GUT, `--export-release` |
| Web (Three.js + WebGPU) | `web.ts_module` (`src/game`), `web.vitest` (`tests/`) | `web.scene_module` | `mesh.gltf_game_ready` (`public/models`) | `build.web` (zip `dist/`) | `npm run typecheck`, Vitest, Vite build, Playwright smoke + screenshot |

`mesh.gltf_game_ready` giống `mesh.fbx_game_ready` nhưng bắt buộc glTF/GLB, Y-up, đơn vị mét. `build.web` phải mở được bằng Chromium headless, không lỗi console trong N giây, và vẽ được ít nhất một frame (WebGPU hoặc WebGL2).

Contract của thể loại khác bổ sung khi có DAG template mới (tài liệu gốc mục 20 ghi là chưa quyết).
