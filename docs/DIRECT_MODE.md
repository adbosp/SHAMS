# Direct Mode (ADR-121)

Run one prompt on one model or preset, without the planner or a Task DAG. The task still goes through the
normal runtime: scheduler, GPU lease (one model on the GPU at a time, the previous one unloaded), worker,
artifact registry, QC and event log.

## Use it

- App: `shams ui` → **Direct Mode** → choose a preset, type a prompt, set parameters, **Chạy**. Tasks live in
  `<workspace>/_direct`.
- CLI:
  ```
  shams presets
  shams direct --preset coder "Write a Python function is_prime(n)"
  shams direct --preset image "a red lighthouse at sunset" --param seed=7 --param width=768
  shams direct --preset qwen35-heretic-q4 "..." --param temperature=0.7 --param research=true
  ```
- API: `GET /api/direct/presets`, `POST /api/direct/run {preset, params, confirm_adult}`,
  `GET /api/direct/tasks`, `GET /api/direct/tasks/{id}`, `GET /api/direct/artifact/{sha256}`.

## Presets

Every resolved role (`models.roles`) is a preset with the role's name. Add more under `models.presets`
(see `shams/examples/local_models.settings.json`):

```json
"presets": {
  "qwen35-heretic-q4": {"provider": "ollama", "model": "hf.co/darrellbest/Qwen3.5-9B-Heretic-GGUF:Q4_K_M",
                        "capabilities": ["chat", "text_generation"], "execution_modes": ["direct"],
                        "autonomous_default": false},
  "moss_image": {"provider": "comfyui", "workflow": "moss_image", "execution_modes": ["direct"],
                 "params": {"text_encoder": "...", "vae": "..."}},
  "moss_image_nsfw_lora": {"provider": "comfyui", "workflow": "moss_image_lora", "execution_modes": ["direct"],
                           "content_policy": {"adult": true}}
}
```

Fields: `kind` (chat, image, scene3d; default from the provider), `provider`, `model` or `workflow`,
`capabilities`, `execution_modes` (`direct`, `autonomous`), `autonomous_default`, `params` (defaults and model
files), `num_ctx`, `vram_mb`, `lane`, `research`, `content_policy`, `input_schema`, `outputs`, `max_attempts`.

A preset is offered to the planner (Autonomous Mode) only when `autonomous_default: true` **and**
`"autonomous"` is in `execution_modes`.

## Adult-only presets

- Off unless `general.allow_adult_content: true`, and every run needs the confirmation
  "I am 18 or older, and the content involves only fictional, consenting adults".
- Never autonomous: settings that try are rejected; the worker only has the `direct.<id>` capability.
- Prompts about minors, non-consent or abuse, and sexual imagery of real, identifiable people are refused
  (English and Vietnamese patterns); a negative prompt against minors is always added. Refusals are logged as
  `policy.refused`. The filter lowers risk; it is not a guarantee.

## Research

`"research": {"enabled": true}` gives models with tool calling two read-only tools, `research__web_search` and
`research__web_fetch`. They reach only the public web (local and private addresses are refused, also after
redirects), cap the size, honour `allow_domains` / `deny_domains`, log every URL and mark web text as untrusted.
`search_url` can point at a SearXNG instance (`...&format=json`). Turn it off per task with `research: false`.

## 3D scenes

Simple scenes: the 3D model writes a JSON scene description; SHAMS builds the GLB and renders evidence in pure
Python. Complex scenes (modifiers, booleans, sculpting, rigs, terrain, vehicles...) go to Blender. Force one
with the task parameter `builder` (`json` or `blender`).
