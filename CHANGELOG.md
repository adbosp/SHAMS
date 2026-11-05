# Changelog

## Unreleased

**SHAMS Desktop Client** (ADR-122)
- Desktop app in `apps/desktop/` (Tauri 2 + React + TypeScript + Tailwind): New Chat, Chat History, Direct AI, Autonomous Workflow, Projects, Media Gallery, Model Manager, Settings; light/dark themes. The Rust shell starts, health-checks and stops the Python Core Service (`shams desktop-backend`).
- Streamed chat with Markdown, code blocks, attachments, thinking, and grouped blocks: tool calls, images/videos/3D, links. History in SQLite; search, pin, archive; survives restarts.
- Coding agent in a local Project (folder + environment): workspace tools, three permission modes, approvals with diff/command preview, per-turn checkpoints and revert, tool log.
- Model Manager: Ollama/ComfyUI discovery, SHAMS inventory matched against installed files, register/enable/disable/remove presets, Ollama load/unload with keep_alive, ComfyUI start and free.
- Process-wide GPU gate shared by chats, Direct Mode and Autonomous runs; resource monitor; job cancel.
- Video preset kind (animated WebP), Wan 2.1 1.3B and generic SDXL checkpoint workflows.

**Direct Mode and presets** (ADR-121)
- Direct Mode: pick a model or preset, enter a prompt, run one task without planner or DAG. It runs on the same scheduler, GPU lease, model switching, workers, artifact registry, QC and event log. App view "Direct Mode", API `/api/direct/*`, CLI `shams presets` and `shams direct`.
- Presets in the Model Registry (`models.presets`, plus every resolved role): capability, provider, resources, input/output schema, execution modes. Direct-only presets are never picked by Autonomous Mode.
- Content policy for adult-only presets: opt-in setting, per-run confirmation, refusal of minors / non-consent / real people, logged refusals. Adult presets can never be autonomous.
- MOSS-Image ComfyUI workflows (base and LoRA). Preflight now checks every choice value, not only model files.
- MOSS-Image workflows follow the official ComfyUI SFT template (no extra shift node, 25 steps, cfg 2.5, er_sde); needs ComfyUI 0.42+. The old graph produced noise.
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
