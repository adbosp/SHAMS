"""ComfyUI provider (ADR-120): image generation through the ComfyUI HTTP API.

SHAMS does not reimplement diffusion: it fills a workflow template (ComfyUI *API format* JSON with
``{{placeholders}}``), checks that every node type and model file the workflow needs is installed,
queues it on ``/prompt``, waits on ``/history`` and downloads the images from ``/view``.

Lifecycle: ``health`` (``/system_stats``), ``resources`` (VRAM per device), ``unload`` (``/free``,
so an LLM can use the GPU next), optional ``autostart`` of a local ComfyUI with a launch command.
"""
from __future__ import annotations

import copy
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from shams.providers import ProviderError, _get_json, _open, _post_json
from shams.workers import base_handoff, output_plan

BUILTIN_WORKFLOWS = Path(__file__).resolve().parent / "workflows"

# node class -> input that names a model file, and the models/ sub-folder it lives in
LOADER_FIELDS = {
    "CheckpointLoaderSimple": ("ckpt_name", "checkpoints"),
    "UNETLoader": ("unet_name", "diffusion_models"),
    "CLIPLoader": ("clip_name", "text_encoders"),
    "DualCLIPLoader": ("clip_name1", "text_encoders"),
    "VAELoader": ("vae_name", "vae"),
    "LoraLoader": ("lora_name", "loras"),
    "LoraLoaderModelOnly": ("lora_name", "loras"),
}


def fill(graph: dict, params: dict[str, Any]) -> dict:
    """Replace ``"{{name}}"`` values (keeping the parameter's type) and ``{{name}}`` inside strings."""
    def sub(v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("{{") and s.endswith("}}") and s[2:-2].strip() in params:
                return params[s[2:-2].strip()]
            for k, val in params.items():
                v = v.replace("{{" + k + "}}", str(val))
            return v
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        return v
    return sub(copy.deepcopy(graph))


class ComfyUI:
    name = "comfyui"
    supports_tools = False

    def __init__(self, base_url: str = "http://127.0.0.1:8188", root: str | None = None,
                 workflows: dict[str, str] | None = None, timeout: float = 900.0, poll_seconds: float = 1.0,
                 launch: list[str] | None = None, launch_cwd: str | None = None, autostart: bool = False,
                 start_timeout: float = 180.0, **_: Any):
        self.base_url = base_url.rstrip("/")
        self.root = root
        self.workflows = dict(workflows or {})
        self.timeout, self.poll = float(timeout), float(poll_seconds)
        self.launch, self.launch_cwd, self.autostart = launch, launch_cwd, autostart
        self.start_timeout = float(start_timeout)
        self.client_id = uuid.uuid4().hex
        self._proc: subprocess.Popen | None = None

    # ------------------------------------------------------------------ health / resources
    def system_stats(self) -> dict:
        return _get_json(f"{self.base_url}/system_stats", {}, 10)

    def health(self) -> str:
        try:
            self.system_stats()
            return "ok"
        except ProviderError:
            return "down"

    def ensure_running(self) -> None:
        if self.health() == "ok":
            return
        if not (self.autostart and self.launch):
            raise ProviderError(f"ComfyUI is not running at {self.base_url}; start it (e.g. run_nvidia_gpu.bat) "
                                f"or set tools.comfyui.autostart + launch")
        self._proc = subprocess.Popen(self.launch, cwd=self.launch_cwd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if self.health() == "ok":
                return
            if self._proc.poll() is not None:
                raise ProviderError(f"ComfyUI exited with code {self._proc.returncode} while starting")
            time.sleep(2)
        raise ProviderError(f"ComfyUI did not answer within {self.start_timeout:.0f}s")

    def resources(self) -> dict:
        devs = self.system_stats().get("devices", [])
        return {"devices": [{"name": d.get("name"), "type": d.get("type"),
                             "vram_total_mb": int(d.get("vram_total", 0)) // 2**20,
                             "vram_free_mb": int(d.get("vram_free", 0)) // 2**20} for d in devs]}

    # ------------------------------------------------------------------ discovery
    def object_info(self, node_class: str) -> dict:
        return _get_json(f"{self.base_url}/object_info/{urllib.parse.quote(node_class)}", {}, 30).get(node_class, {})

    def options(self, node_class: str, field: str) -> list[str] | None:
        """Allowed values of a combo input, or None when the node type is not installed."""
        info = self.object_info(node_class)
        if not info:
            return None
        spec = {**info.get("input", {}).get("required", {}), **info.get("input", {}).get("optional", {})}.get(field)
        if not spec:
            return []
        first = spec[0]
        if isinstance(first, list):
            return [str(x) for x in first]
        if first == "COMBO" and len(spec) > 1:
            return [str(x) for x in spec[1].get("options", [])]
        return []

    def list_models(self, folder: str = "checkpoints") -> list[str]:
        try:
            r = _get_json(f"{self.base_url}/models/{folder}", {}, 15)
            if isinstance(r, list):
                return sorted(r)
        except ProviderError:
            pass
        for cls, (fld, sub) in LOADER_FIELDS.items():
            if sub == folder:
                return sorted(self.options(cls, fld) or [])
        return []

    # ------------------------------------------------------------------ workflows
    def workflow(self, name: str) -> dict:
        """A workflow by name (tools.comfyui.workflows, then built-in templates) or by path. Returns
        {"graph": ..., "defaults": {...}, "outputs": [...]}"""
        path = Path(self.workflows.get(name) or name)
        if not path.suffix:
            path = BUILTIN_WORKFLOWS / f"{name}.json"
        if not path.exists():
            raise ProviderError(f"ComfyUI workflow {name!r} not found ({path})")
        doc = json.loads(path.read_text(encoding="utf-8"))
        if "graph" not in doc:
            if "nodes" in doc and "links" in doc:
                raise ProviderError(f"{path} is a UI workflow; export it with 'Save (API Format)'")
            doc = {"graph": doc, "defaults": {}}
        return doc

    @staticmethod
    def _combo(spec: Any) -> list[str] | None:
        if not isinstance(spec, list) or not spec:
            return None
        first = spec[0]
        if isinstance(first, list):
            return [str(x) for x in first]
        if first == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
            return [str(x) for x in spec[1].get("options", [])]
        return None

    def preflight(self, graph: dict) -> list[str]:
        """Problems that would make the prompt fail: missing node types, model files, or choice values the
        installed ComfyUI does not know (e.g. a text-encoder type added in a newer version)."""
        problems, infos = [], {}
        for nid, node in graph.items():
            cls = node.get("class_type")
            if cls not in infos:
                infos[cls] = self.object_info(cls)
            info = infos[cls]
            if not info:
                problems.append(f"node {nid}: node type {cls!r} is not installed in ComfyUI (update ComfyUI "
                                f"or add the custom node)")
                continue
            specs = {**info.get("input", {}).get("required", {}), **info.get("input", {}).get("optional", {})}
            for fld, val in (node.get("inputs") or {}).items():
                if not isinstance(val, str):
                    continue
                opts = self._combo(specs.get(fld))
                if opts is None or val in opts:
                    continue
                if cls in LOADER_FIELDS and LOADER_FIELDS[cls][0] == fld:
                    problems.append(f"node {nid}: model file {val!r} not found in models/{LOADER_FIELDS[cls][1]}")
                else:
                    problems.append(f"node {nid}: {cls}.{fld} = {val!r} is not supported by this ComfyUI "
                                    f"(choices: {', '.join(opts[:8])}{'...' if len(opts) > 8 else ''})")
        return problems

    # ------------------------------------------------------------------ execution
    def queue(self, graph: dict) -> str:
        try:
            r = _post_json(f"{self.base_url}/prompt", {"prompt": graph, "client_id": self.client_id}, {}, 60)
        except ProviderError as e:
            raise ProviderError(f"ComfyUI rejected the workflow: {e}") from None
        if r.get("node_errors"):
            raise ProviderError(f"ComfyUI node errors: {json.dumps(r['node_errors'])[:800]}")
        return r["prompt_id"]

    def wait(self, prompt_id: str) -> dict:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            h = _get_json(f"{self.base_url}/history/{prompt_id}", {}, 30).get(prompt_id)
            if h:
                st = h.get("status") or {}
                if st.get("status_str") == "error":
                    msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
                    detail = msgs[-1][1].get("exception_message", "") if msgs else "execution error"
                    raise ProviderError(f"ComfyUI execution failed: {detail}"[:800])
                if st.get("completed", True) and h.get("outputs") is not None:
                    return h
            time.sleep(self.poll)
        try:
            _post_json(f"{self.base_url}/interrupt", {}, {}, 10)
        except ProviderError:
            pass
        raise ProviderError(f"ComfyUI prompt {prompt_id} did not finish within {self.timeout:.0f}s")

    def view(self, image: dict) -> bytes:
        q = urllib.parse.urlencode({"filename": image["filename"], "subfolder": image.get("subfolder", ""),
                                    "type": image.get("type", "output")})
        req = urllib.request.Request(f"{self.base_url}/view?{q}")
        try:
            with _open(req, 60) as r:
                return r.read()
        except urllib.error.URLError as e:
            raise ProviderError(f"cannot download {image['filename']}: {e}") from None

    def generate(self, workflow: str, params: dict[str, Any] | None = None) -> dict:
        """Run a workflow. Returns {"images": [bytes], "seconds", "prompt_id", "params"}."""
        self.ensure_running()
        doc = self.workflow(workflow)
        p = {**doc.get("defaults", {}), **(params or {})}
        if p.get("seed") in (None, -1, "random"):
            p["seed"] = int.from_bytes(uuid.uuid4().bytes[:6], "big")
        graph = fill(doc["graph"], p)
        problems = self.preflight(graph)
        if problems:
            raise MissingResources(problems)
        t0 = time.monotonic()
        pid = self.queue(graph)
        h = self.wait(pid)
        imgs = [im for out in h.get("outputs", {}).values() for im in out.get("images", [])
                if im.get("type", "output") == "output"]
        if not imgs:
            raise ProviderError("ComfyUI finished but produced no output image")
        return {"images": [self.view(im) for im in imgs], "seconds": round(time.monotonic() - t0, 2),
                "prompt_id": pid, "params": {k: v for k, v in p.items() if k != "prompt"}}

    # ------------------------------------------------------------------ lifecycle hooks for the scheduler
    def load(self, model: str) -> None:            # ComfyUI loads lazily on the first prompt
        return None

    def unload(self, model: str | None = None) -> None:
        try:
            _post_json(f"{self.base_url}/free", {"unload_models": True, "free_memory": True}, {}, 30)
        except ProviderError:
            pass

    def chat(self, messages: list[dict], *, model: str, **opts: Any) -> dict:
        raise ProviderError("comfyui is an image backend; use the comfyui_image worker")


class MissingResources(ProviderError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class ComfyUIImageWorker:
    """Fills a ComfyUI workflow (task title/description as the prompt) and writes the image outputs."""

    def __init__(self, *, entry: dict, attempts_root: Path, provider: Any = None, **_: Any):
        cfg = entry.get("provider_config", {})
        self.provider = provider or ComfyUI(**{k: v for k, v in cfg.items() if k in ("base_url", "workflows", "timeout", "launch", "launch_cwd", "autostart")})
        self.workflow = cfg.get("workflow") or entry["worker"].get("model") or "sdxl_lightning_4step"
        self.params = dict(cfg.get("params") or {})
        self.attempts_root = Path(attempts_root)
        self.model = entry["worker"].get("model") or self.workflow
        self.content_policy = dict(cfg.get("content_policy") or {})
        self.preset = cfg.get("preset")

    def run(self, ctx: dict, worker_ref: dict) -> tuple[list[dict], dict]:
        from shams import policy
        _, plan = output_plan(ctx, self.attempts_root)
        task = ctx["task"]
        tparams = dict(task.get("params") or {})
        prompt = tparams.pop("prompt", None) or task.get("description") or task["title"]
        if ctx.get("goal") and not task.get("preset"):
            prompt = f"{prompt}. Context: {ctx['goal']}"
        params = {**self.params, **tparams, "prompt": prompt}
        if self.content_policy.get("adult"):          # checked again here: defence in depth
            try:
                params = policy.check({"id": self.preset, "kind": "image", "content_policy": self.content_policy},
                                      params, allowed=bool(self.content_policy.get("allowed")),
                                      confirmed=bool(tparams.get("_adult_confirmed") or
                                                     (task.get("params") or {}).get("_adult_confirmed")))
            except policy.PolicyError as e:
                return [], base_handoff(ctx, worker_ref, "BLOCKED", "content policy",
                                        blocked_reason=f"policy: {e}"[:500])
        params.pop("_adult_confirmed", None)
        imgs = [o for o in plan if o["media_type"] in ("image/png", "image/jpeg")]
        if not imgs:
            return [], base_handoff(ctx, worker_ref, "BLOCKED", "no image output declared",
                                    blocked_reason="comfyui worker only writes image/png outputs")
        try:
            r = self.provider.generate(self.workflow, params)
        except MissingResources as e:
            return [], base_handoff(ctx, worker_ref, "BLOCKED", "ComfyUI resources missing",
                                    blocked_reason="tool unavailable: " + "; ".join(e.problems)[:500])
        except ProviderError as e:
            return [], base_handoff(ctx, worker_ref, "FAILED", "ComfyUI failed",
                                    known_issues=[{"issue": str(e), "severity": "high"}], blocked_reason=str(e)[:500])
        files = []
        for o, data in zip(imgs, r["images"]):
            Path(o["abs"]).parent.mkdir(parents=True, exist_ok=True)
            Path(o["abs"]).write_bytes(data)
            files.append(o)
        return files, base_handoff(ctx, worker_ref, "READY_FOR_QC", f"ComfyUI {self.workflow}: {len(files)} image(s)",
                                   usage={"wall_seconds": r["seconds"]},
                                   decisions=[{"decision": f"workflow {self.workflow}", "reason": json.dumps(r["params"])[:300]}])


