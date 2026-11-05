"""Built-in tool integrations for SHAMS (ADR-120): ComfyUI, Stable Fast 3D, Blender.

Kept outside ``shams/`` so the core names no tool (ADR-117). ``shams.plugins.load_extensions()``
imports this package and calls ``register()``, which plugs in:

* providers ``comfyui``, ``stable_fast_3d``; workers ``scene3d``, ``comfyui_image``, ``sf3d``;
* model-registry backends ``comfyui`` and ``stable_fast_3d`` for the ``image`` / ``image_to_3d`` roles,
  and a hook giving the ``scene3d`` role its Blender path;
* the QC check type ``blender_import``; probes shown by ``shams models``.
"""
from __future__ import annotations

from pathlib import Path

_DONE = False


class _ComfyBackend:
    def bind(self, reg, role: str, cfg: dict):
        from shams.models import RoleBinding
        c = (reg.tools.get("comfyui") or {})
        wf = cfg.get("workflow") or cfg.get("model") or "sdxl_lightning_4step"
        return RoleBinding(role, wf, wf if c else None, "ok" if c else "not_configured",
                           how="tools.comfyui", backend="comfyui", config=cfg)

    def worker(self, reg, b) -> dict:
        c = dict(reg.tools.get("comfyui") or {})
        params = {**((c.get("params") or {}).get(b.model_id) or {}), **(b.config.get("params") or {})}
        cfg = {k: v for k, v in {"base_url": c.get("base_url"), "workflows": c.get("workflows"),
                                 "workflow": b.model_id, "params": params,
                                 "launch": c.get("launch"), "launch_cwd": c.get("launch_cwd"),
                                 "autostart": c.get("autostart"), "timeout": c.get("timeout"),
                                 "impl": "comfyui_image"}.items() if v}
        return {"kind": "tool", "provider": "comfyui", "model": b.model_id, "load_adapter": "provider",
                "vram_mb": int(b.config.get("vram_mb", 8000)), "config": cfg}


class _SF3DBackend:
    def bind(self, reg, role: str, cfg: dict):
        from shams.models import RoleBinding
        c = reg.tools.get("stable_fast_3d") or {}
        if not c:
            return RoleBinding(role, "stable-fast-3d", None, "not_configured", how="tools.stable_fast_3d",
                               backend="stable_fast_3d", config=cfg)
        from shams_tools.sf3d import StableFast3D
        h = StableFast3D(**{k: v for k, v in c.items() if k != "enabled"}).health_report()
        return RoleBinding(role, "stable-fast-3d", "stable-fast-3d" if h["status"] == "ok" else None,
                           "ok" if h["status"] == "ok" else "missing", how="; ".join(h["problems"]) or "tools.stable_fast_3d",
                           backend="stable_fast_3d", config=cfg)

    def worker(self, reg, b) -> dict:
        c = dict(reg.tools.get("stable_fast_3d") or {})
        cfg = {k: v for k, v in c.items() if k in ("repo", "python", "weights", "device", "texture_resolution",
                                                   "remesh", "timeout", "env", "force")}
        cfg["impl"] = "sf3d"
        return {"kind": "tool", "provider": "stable_fast_3d", "model": "stable-fast-3d", "load_adapter": "none",
                "vram_mb": int(b.config.get("vram_mb", 7000)), "config": cfg}


def _scene3d_hook(reg, w: dict) -> None:
    from shams_tools.blender import blender_from
    bl = blender_from(reg.settings)
    if bl:
        w["config"]["blender"] = bl
    w["config"].setdefault("impl", "scene3d")


def _probe_blender(settings: dict) -> dict:
    from shams_tools.blender import blender_from
    p = blender_from(settings)
    return {"status": "ok" if p else "missing", "path": p}


def _probe_comfyui(settings: dict) -> dict:
    c = (settings.get("tools") or {}).get("comfyui")
    if not c:
        return {"status": "not_configured"}
    from shams_tools.comfyui import ComfyUI
    prov = ComfyUI(**{k: v for k, v in c.items() if k in ("base_url", "workflows")})
    out = {"base_url": prov.base_url, "status": prov.health()}
    if out["status"] == "ok":
        try:
            out["resources"] = prov.resources()
            out["checkpoints"] = prov.list_models("checkpoints")
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
    return out


def _probe_sf3d(settings: dict) -> dict:
    c = (settings.get("tools") or {}).get("stable_fast_3d")
    if not c:
        return {"status": "not_configured"}
    from shams_tools.sf3d import StableFast3D
    return StableFast3D(**{k: v for k, v in c.items() if k != "enabled"}).health_report()


def register() -> None:
    global _DONE
    if _DONE:
        return
    _DONE = True
    from shams import models, providers, workers
    from shams.verification.checkers import register_check_type
    from shams_tools import blender, comfyui, sf3d

    providers.register_provider("comfyui", comfyui.ComfyUI)
    providers.register_provider("stable_fast_3d", sf3d.StableFast3D)
    workers.register_worker("scene3d", blender.Scene3DWorker)
    workers.register_worker("comfyui_image", comfyui.ComfyUIImageWorker)
    workers.register_worker("sf3d", sf3d.StableFast3DWorker)
    workers.worker_only_keys("blender", "blender_timeout")
    models.register_backend("comfyui", _ComfyBackend())
    models.register_backend("stable_fast_3d", _SF3DBackend())
    models.register_role_hook("scene3d", _scene3d_hook)
    models.register_probe("blender", _probe_blender)
    models.register_probe("comfyui", _probe_comfyui)
    models.register_probe("stable_fast_3d", _probe_sf3d)
    register_check_type("blender_import", blender.check_blender_import)


WORKFLOWS = Path(__file__).resolve().parent / "workflows"
