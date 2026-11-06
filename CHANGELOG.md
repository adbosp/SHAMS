# Changelog

## Unreleased

- Web Chat hiển thị câu trả lời từng đoạn khi Ollama đang sinh chữ. Model trả một lần có hiệu ứng hiện dần ngắn, và lịch sử vẫn lưu câu trả lời hoàn chỉnh.

- Research cho worker và Direct Mode: tìm kiếm SearXNG với fallback DuckDuckGo/Bing RSS, đọc và đối chiếu nguồn công khai, ghi URL thực sự đọc, cùng browser chỉ đọc tùy chọn. Transport kiểm từng redirect trước khi kết nối và ghim IP đã xác minh. Xem `docs/RESEARCH.md`.

- Web UI: chat văn bản theo từng cuộc trò chuyện, chọn model, lưu lịch sử trên trình duyệt và giữ ngữ cảnh khi hỏi tiếp. Luồng chat web không gọi công cụ và không mở màn hình điều phối dự án.

- 3D briefs: the planner turns the request into detailed English build descriptions before the 3D model runs: each character (height, hair, skin, every clothing item with colour) and the play area (`brief.environment`: ground, props with counts, sizes, colours, layout). Vietnamese slang such as 'cưa gái' is explained as flirting, not a saw.

- Settings > Chung: switches for 18+ presets (`general.allow_adult_content`) and opening a finished game (`general.open_game`). Direct Mode links to the switch instead of asking to edit JSON.

- Direct Mode is a chat: each run is a turn (prompt, then the model's reply), with the model picker and parameters in the composer (Enter sends). Outputs show inline: images, video and audio play in the chat, other files are download cards. Markdown is rendered and code is framed with its language and a copy button, in Direct Mode and in the project chat.

- Autonomous mode runs to the end without approvals and is the default for new projects (`general.default_mode`). A task out of repair attempts is accepted with its last outputs by `autopilot` (event payload lists the failed checks; the play link warns about them); criteria without a checker no longer stop it. A task that produced no output at all still waits in HUMAN_REVIEW. Guided and collaborative modes are unchanged.

- Game code goes to the 3D/game model: built-in role `game_coder` (capabilities `code.game.*`, `integration.game`) is optional: the general coder writes game code unless `models.roles.game_coder` is set (the 4B 3D model wrote plans instead of code in a real run). The game template's gameplay, HUD and integration tasks use `${game_code}` / `integration.game`. The scheduler leases a task to the most specific worker (or the one named by `provider_hint`); after 3 failures the repair router switches to the next best fit (the general coder) and grants 2 more attempts instead of stopping at HUMAN_REVIEW.

- Game template simplified (12 tasks): no SDXL concept images and no Stable Fast 3D. The 3D scene model builds both characters and the play area as JSON primitives (`mesh.procedural`, `builder: json`) from the character brief; the ground texture task is replaced by `task.environment_model` (`models/Environment/environment.glb`). The 3D model now receives text inputs (briefs) in its prompt. Web scene hint uses relative asset paths.
- Play link: `/play/<task>/` redirects to a per-build static server on 127.0.0.1 that serves the game from its root, so absolute asset paths (`/models/...`) load too.
- App: delete a project from the workspace list (trash icon, confirmation dialog). Deletion is permanent and logged to `<workspace>/_logs/deletions.log` (time, project, folder, goal).

- App: when a build task with a playable copy is accepted, SHAMS posts a local link in the project chat (`/play/<task>/` on the app's own server) with a "Chơi thử game" button and opens it in the browser (`general.open_game`, default on). A build accepted by hand while a check failed is announced with a warning naming the failed checks.
- Web scene hint: WebGLRenderer (the model used `THREE.WebGPU`, which does not exist, and the game showed a black screen), load both characters, camera above and looking down, stronger lights, HUD attached to the container.

- Web unit tests: Jest-style calls (`jest.fn`, `jest.spyOn`) run on Vitest through a setup file that maps `jest` to `vi`. Engine profiles get a `test_hint` (`${test_hint}` in templates); for Web it tells the coder to use Vitest's `vi` and keep tests next to the module they test. Template tokens are JSON-escaped, so engine text may contain quotes.

**Direct Mode and presets** (ADR-121)
- Direct Mode: pick a model or preset, enter a prompt, run one task without planner or DAG. It runs on the same scheduler, GPU lease, model switching, workers, artifact registry, QC and event log. App view "Direct Mode", API `/api/direct/*`, CLI `shams presets` and `shams direct`.
- Presets in the Model Registry (`models.presets`, plus every resolved role): capability, provider, resources, input/output schema, execution modes. Direct-only presets are never picked by Autonomous Mode.
- Content policy for adult-only presets: opt-in setting, per-run confirmation, refusal of minors / non-consent / real people, logged refusals. Adult presets can never be autonomous.
- MOSS-Image ComfyUI workflows (base and LoRA). Preflight now checks every choice value, not only model files.
- MOSS-Image workflows follow the official ComfyUI SFT template (no extra shift node, 25 steps, cfg 2.5, er_sde); needs ComfyUI 0.42+. The old graph produced noise.
- Game template QC runs for real: GLB checks (triangle budget, UV, size) for character meshes, a preview render judged by the vision model, power-of-two texture check, and a Node.js syntax check for Web/Three.js code. Unity/Unreal/Godot compile stays NOT_VERIFIED until the engine runner exists.
- 3D asset tasks (mesh.*) build only their one asset; the project goal is passed as background.
- App: native crashes write every thread's stack to `<workspace>/_logs/crash.log`; run-loop errors go to `_logs/run_loop_errors.log` and the project chat.
- Ollama tool calls go through native /api/chat, so `num_ctx` applies (the OpenAI-compatible endpoint ran with a 4096-token context and cut long task prompts).
- A model reply without any declared output is a task failure (repair loop) and the raw reply is kept as `_model_reply.txt`; an invalid handoff fails the attempt instead of stopping the run loop, also on resume.
- The app no longer resumes an unfinished project when it opens it; a "Tiếp tục chạy" button does that. Missing-worker notes use the scheduler's capability matching.
- Fixed the app crash (access violation): the event store closed its SQLite connection while another thread was reading it during a project switch. Closing now waits for readers, reads after close raise a clean error, project switches run one at a time and the UI state retries.
- A tool call the model writes as plain JSON text is recognised and executed (local models often do this); the last tool round asks for the final answer.
- Scene3D: when a separate model writes Blender scripts, the two models are swapped on the GPU, never loaded together, and the lease covers the larger one. The Blender prompt lists common API pitfalls.
- 3D evidence: SHAMS always frames its own camera on the meshes; meshes without UVs get Smart UV Project before export. GLB size checks apply node transforms (world size). Character look checks are required; the NPC and the ground texture get a vision check; texture prompts ask for a seamless top-down texture and no longer include the project goal.
- Game template characters: SDXL draws a full-body concept image (`image.concept`), then Stable Fast 3D builds the mesh from it (`mesh.image_to_3d`). A plan step that asks for image-to-3D skips the 3D route heuristic. SF3D remeshes to the triangle budget when its run.py supports a vertex target; Blender then scales the mesh to `params.height_m`, puts the feet at the origin and renders the preview.
- Character concepts: a planner step writes a short English appearance brief per character from the project request; the image model gets the brief first (no more Vietnamese goal in SDXL prompts) and the negative prompt rules out character sheets, turnarounds and several views. Visual QC sees the brief and now fails more than one figure; GLB checks can cap width/height (`max_width_to_height`, 0.9 for characters).
- SF3D writes its command line and log to `_sf3d/sf3d_run.log` and the handoff. Previews of textured meshes render with their textures. The PNG noise check also needs a large absolute neighbour difference, so fine low-contrast ground textures are no longer called noise.
- `exists` criteria without a pack checker are checked for real (every output present and non-empty) instead of staying NOT_VERIFIED; the character brief also needs 40+ characters. A test checks that every web game criterion has a checker. Character briefs must describe clearly adult people in adult clothing; concept negatives exclude child/teen looks and school uniforms, and the concept look check fails anyone who looks under age.
- Character briefs describe appearance only and follow the roles in the request; sentences about background or style are dropped from the image prompt (`params.brief_skip`), the negative excludes scenery, and the concept look check judges only the must-haves (one person, full body, plain background, adult, gender and main outfit), not accessories or exact colours. The vision QC model is unloaded after each check so generators get the GPU.
- A text model that answers BLOCKED without writing its outputs (e.g. after an empty web search) is a failed attempt and goes through the repair loop; the worker prompt says tools are optional. PNG checks can require the subject to stay inside the frame (`max_edge_subject`): concepts with feet cut off fail. Character look checks ask for both feet; width/height cap 0.7. SF3D previews are taken from the character's front (`preview_dir`). The full SDXL prompt and negative are kept in the handoff. Character briefs: in dating games the NPC is the person being courted, with the gender the request names.
- Web game unit tests run for real: `shams toolchain install web` installs pinned Vitest 3.2.4 + Three.js 0.178 into `~/.shams/toolchains/web` (never into a project); the Game Pack `unit_test` checker copies the task's code, tests and upstream code into a temporary folder there and runs Vitest. Without the toolchain it stays NOT_VERIFIED. Spawner, core mechanic and HUD tasks now declare a test file too.
- Research: a search page that is not a result page (e.g. HTTP 202 bot check) is reported as "search unavailable, write from your own knowledge" instead of "no results"; a task can opt out of research (`params.research: false`, set for the game scaffold). The concept border check compares each border with the blend of its corner colours, so studio gradients pass; concepts ask for empty space around the figure and briefs avoid plant words, which SDXL painted into the background.
- Research can use a SearXNG instance on this PC (e.g. with Google as the engine): the configured search host is the only local address allowed, for searches only. `scripts/searxng/` has a local-only settings file and Docker steps.
- Vitest runs with globals (tests that forget to import describe/it still run). Text inputs from upstream code carry their export lines (`exports`), so the next worker imports default vs named exports correctly. Character concepts render portrait (768x1344) and the border check looks only at top/bottom (`edge_sides`), where cropping shows; side shadows no longer fail. SearXNG settings keep the container's default listener; the Docker port mapping limits it to this PC.
- Image prompts: `params.prompt_prefix` goes before everything (the concept puts "plain white seamless background" first, since SDXL-Lightning at cfg 1 barely follows negatives); `brief_max_sentences` keeps only the first sentences of a brief, and briefs may not mention places or words that make people look young. The border-check message now says "cropped or background not plain". Unit-test criteria tell the coder to keep tested logic free of DOM/renderer code (tests run in Node).
- Web games build and run for real: a built-in `build` role (no setup; uses the web toolchain) builds with Vite, adds `index.html` + `src/main.ts` that call the scene module's `start(container)`, writes the zip and an unpacked `play/` folder. New check types `web_zip` and `web_smoke`: the integration scene and the final build are opened in a headless Chromium/Edge driven over the DevTools protocol (Node, no packages) and fail on uncaught errors or a blank screen. Engine profiles carry `scene_hint`, `run_check`, `build_check`; other engines stay NOT_VERIFIED.
- Game template: audio no longer blocks integration (no audio worker yet); HUD, core mechanic and the build get the whole import chain as inputs; FPS is optional. Brief filtering matches whole words. Tool workers that need no setup do not switch new projects out of test mode.
- Single 3D assets must be one connected piece: detached parts (a floating head, loose arms) go back to the model as a fix round; the character look check asks for connected body parts.
- PNG checks reject random-noise images (neighbour/std "roughness"); self-test 9e adds visual QC.
- Read-only web research tools for models (public web only, untrusted-content marking, URL log).
- Simple 3D scenes: the 3D model writes scene JSON and SHAMS builds the GLB and a render without Blender. Blender is used for complex scenes.

**Local models by role** (ADR-120)
- Model Registry: roles planner, coder, scene3d, visual_qc, embedding, image, image_to_3d. Preferred model names are mapped onto the IDs Ollama really has; one worker per role is generated. `shams models` shows the mapping.
- Ollama: native chat (thinking off, `num_ctx`, JSON mode, images), embeddings, `/api/ps` resource reporting, unload that waits until VRAM is free.
- Tool integrations in `shams_tools/`: ComfyUI (SDXL-Lightning and Z-Image-Turbo workflow templates, preflight of nodes and model files), Stable Fast 3D (reference image + complex object only), Blender 3D Scene worker (coding model writes Blender Python, render + GLB, bounded fix loop).
- QC sees real files: GLB, PNG, scene report, Blender import, Python compile, pytest, vision-model checks.
- LLM planner with validation feedback and template fallback (`shams plan --llm`).
- `shams selftest`: integration tests on the real machine with PASS / FAIL / BLOCKED / SKIPPED and evidence.
- ADR-119: desktop client (Tauri + React) over the Core Service API.

## 2.0.0

SHAMS 2.0 is the first release meant for other people to install and use.

**Core and domains**
- The core no longer knows any product domain. Each domain is a Domain Pack (ADR-117):
  - Software Pack: Python, TypeScript.
  - Game Pack: Unity, Unreal Engine 5, Godot, Three.js/WebGPU, custom engines.
- Add your own packs with `register_pack()` or the `shams.packs` entry point.
- Python namespace renamed from `studio` to `shams`.

**App** (`shams ui`)
- Workspace with several projects; the first chat message creates a project.
- Settings with eight tabs: General, Permissions, Models & API, MCP, Hooks, Hardware, Environment, Config files.
- Real GPU/VRAM, GPU load, RAM and CPU readings. The demo mode is gone.

**Models and tools** (ADR-116, ADR-118)
- Any local model or API works through reusable connections: Ollama, llama.cpp, LM Studio, vLLM, OpenAI-compatible, OpenAI, Anthropic, Gemini, OpenRouter, Groq, DeepSeek, Mistral, Together, xAI, command line tools, plugins.
- MCP client with stdio and HTTP transports. Reads `.mcp.json`. LLM workers call MCP tools under permissions (default / full access / read only, allow and deny lists).
- Hooks run your commands on events such as `run.finished` or `qc.failed`.
- Layered settings: user, project and project-local files. Secrets are never written to disk.

**CLI**
- New commands: `shams packs`, `shams hardware`, `shams settings`, `shams mcp`, `shams --version`.
- `init` and `plan` accept `--domain` and `--opt key=value`.
- `run` exit codes: 0 done, 1 waiting for a human, 2 error.
