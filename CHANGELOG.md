# Changelog

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
