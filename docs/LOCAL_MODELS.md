# Local models, image and 3D tools (ADR-120)

SHAMS uses local models by **role**. You say which model you prefer for each role. SHAMS discovers what is
really installed and maps your preference onto it. No model ID is written in the orchestration code.

## 1. Roles

| Role | Default capabilities | Typical model | Backend |
|---|---|---|---|
| `planner` | `plan.*`, `architect.*`, `debug_analysis`, `semantic_review` | reasoning LLM (e.g. `qwen3.5:9b`) | Ollama |
| `coder` | `code.*`, `integration*` | coding LLM (e.g. `qwen2.5-coder:14b`) | Ollama |
| `scene3d` | `scene.*`, `mesh.procedural`, `mesh.text_to_3d` | coding LLM that knows Blender Python | Ollama + Blender |
| `visual_qc` | `visual_review*` (also the `vlm` QC check) | vision LLM (e.g. `qwen3-vl:4b`) | Ollama |
| `embedding` | `embed.text`, `retrieval.*` | embedding model | Ollama (no scheduler worker) |
| `image` | `image.*` | a ComfyUI workflow | `comfyui` |
| `image_to_3d` | `mesh.image_to_3d` | Stable Fast 3D | `stable_fast_3d` |

Override capabilities per role with `"capabilities": [...]`, or disable a role with `"enabled": false`.

## 2. Configure

Copy [`shams/examples/local_models.settings.json`](../shams/examples/local_models.settings.json) into
`~/.shams/settings.json` (all projects) or `<project>/.shams/settings.json`, and adjust the paths.

```
shams models          # Ollama models, role -> installed ID, Blender / ComfyUI / SF3D status
shams selftest        # real integration tests on this machine (report in <workspace>/_selftest/)
shams plan <dir> --llm   # the planner model writes the Task DAG (template fallback)
```

Name matching: alias → exact → case-insensitive → every token of the preferred name present in the
installed name (`qwen3.5-4b-coder:Q8` → `hf.co/ccore/Qwen3.5-4B-3D-Coder:Q8_0`; size tokens such as `9b`
must match exactly). Ambiguous matches are reported, never guessed. Use `models.aliases` to pin one.

## 3. GPU use

* One GPU: tasks run one at a time. Before a model loads, the previous one is unloaded and SHAMS waits
  until Ollama really released it (`/api/ps`). ComfyUI is asked to free VRAM (`/free`) when the lane
  switches. Visual QC unloads other Ollama models first.
* VRAM per role worker is estimated from the model size and `num_ctx`; set `vram_mb` to override.
* Several GPUs or remote APIs: the scheduler runs lanes in parallel.

## 4. 3D policy

Cheapest first: reuse an existing asset → primitives → procedural (Blender Python) → modular kit →
placeholder → **Stable Fast 3D only with a reference image and a genuinely complex object**. The 3D Scene
worker is a coding model writing Blender Python; it does not generate neural meshes. The SF3D worker refuses
(BLOCKED) when there is no reference image or when a cheaper route is enough.

## 5. QC checks for assets

Add `check.type` to an acceptance criterion:

| type | kind (suggested) | what is checked |
|---|---|---|
| `glb` | `schema` | GLB container, accessors, vertices, triangles, UV, texture, material, bounding-box scale |
| `image` | `screenshot` | PNG size, not blank (pixel spread), colour count |
| `scene_report` | `metric` | mesh objects, camera, light, materials, non-manifold edges, triangle budget |
| `blender_import` | `runtime_log` | the GLB imports into Blender and renders a non-blank image |
| `python` | `compile` | every Python output compiles |
| `pytest` | `unit_test` | pytest on the outputs (upstream Python inputs on the path) |
| `vlm` | `visual` | the vision model judges the image against the criterion (score ≥ `min_score`) |

Flow: worker → staging (attempt folder) → validation (stage 1) → QC (stages 2–5) → accepted → handoff to
dependants. Failures are classified (`asset_validation`, `visual_mismatch`, `tool_unavailable`,
`provider_error`, …) and retried within `max_attempts`.

## 6. ComfyUI workflows

Built-in templates (ComfyUI API format, `{{placeholders}}`): `sdxl_lightning_4step`, `z_image_turbo`
(`shams_tools/workflows/`). Add your own with `tools.comfyui.workflows: {"name": "path/to/api.json"}` — export
them from ComfyUI with *Save (API Format)*. Before queueing, SHAMS checks that every node type and model file
exists; missing pieces end the task as BLOCKED with the exact file names.

## 7. Self-test results

`shams selftest` writes `report.md` / `report.json` and the evidence (scripts, GLB, renders, images).
PASS = ran and every check passed; FAIL = ran and a check failed; BLOCKED = a prerequisite is missing, nothing
ran; SKIPPED = not selected or depends on a step that did not pass.
