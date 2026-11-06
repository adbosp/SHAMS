# Changelog

## Unreleased

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
