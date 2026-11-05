"""Model Manager (ADR-122): what is really installed, what is registered, and the controls around it.

* Discovery: Ollama (``/api/tags`` + ``/api/show`` + ``/api/ps``), ComfyUI (model folders through
  ``/models/<folder>``, built-in and configured workflows) and the probes extensions register.
* Inventory: ``inventory.json`` lists models SHAMS knows how to use; each entry is matched against the
  discovery result and reported as ``installed`` / ``missing`` / ``incomplete`` / ``unknown`` (backend down).
  Being listed never means being installed.
* Registry edits go to the user settings (``models.presets``) — add, remove, enable/disable, configure.
  Specialty models are registered direct-only (never picked by Autonomous Mode) unless the user changes it.
* Lifecycle: Ollama load/unload with an explicit ``keep_alive``; ComfyUI start (configured launch command)
  and ``/free``. Nothing is downloaded and no process is killed.
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

from shams import providers
from shams import settings as settings_mod

INVENTORY = Path(__file__).resolve().parent / "inventory.json"
COMFY_FOLDERS = ["checkpoints", "diffusion_models", "loras", "vae", "text_encoders", "clip"]


def _settings() -> dict:
    return settings_mod.load(None)


def ollama_provider(s: dict | None = None):
    o = ((s or _settings()).get("models") or {}).get("ollama") or {}
    return providers.create("ollama", {k: v for k, v in {"base_url": o.get("base_url")}.items() if v})


def comfy(s: dict | None = None):
    c = ((s or _settings()).get("tools") or {}).get("comfyui")
    if not c:
        return None
    from shams_tools.comfyui import ComfyUI
    return ComfyUI(**{k: v for k, v in c.items() if k in ("base_url", "workflows", "launch", "launch_cwd",
                                                          "autostart", "start_timeout")})


def discover_comfyui(s: dict | None = None) -> dict:
    cu = comfy(s)
    if cu is None:
        return {"status": "not_configured", "folders": {}, "workflows": []}
    from shams_tools.comfyui import BUILTIN_WORKFLOWS
    wfs = sorted({p.stem for p in BUILTIN_WORKFLOWS.glob("*.json")} | set(cu.workflows))
    out: dict[str, Any] = {"status": "down", "base_url": cu.base_url, "folders": {}, "workflows": wfs,
                           "autostart": bool(cu.autostart and cu.launch)}
    if cu.health() != "ok":
        return out
    out["status"] = "ok"
    try:
        out["resources"] = cu.resources()
    except providers.ProviderError:
        pass
    for f in COMFY_FOLDERS:
        try:
            out["folders"][f] = cu.list_models(f)
        except providers.ProviderError:
            out["folders"][f] = []
    return out


def _files_match(files: list[str], patterns: list[str]) -> list[str]:
    low = [(f, f.lower().replace("\\", "/")) for f in files]
    return sorted({f for f, lf in low for p in patterns if fnmatch.fnmatch(lf.rsplit("/", 1)[-1], p.lower())})


def inventory(ollama: dict, comfyui: dict, registry_presets: list[dict], probes: dict) -> list[dict]:
    from shams.models import match_model
    data = json.loads(INVENTORY.read_text(encoding="utf-8"))
    names = [m["name"] for m in ollama.get("models", [])]
    used = {(p.get("model") or "").lower() for p in registry_presets} | {(p.get("wanted") or "").lower() for p in registry_presets}
    out = []
    for e in data["models"]:
        row = {k: e.get(k) for k in ("id", "label", "backend", "kind", "use", "specialty", "adult", "workflow")}
        if e["backend"] == "ollama":
            if ollama.get("status") != "ok":
                row.update(status="unknown", detail="Ollama is not reachable")
            else:
                hit = None
                for want in e.get("match", []):
                    mid, how = match_model(want, names)
                    if mid:
                        hit = (mid, how)
                        break
                row.update(status="installed" if hit else "missing", installed_id=hit[0] if hit else None,
                           detail=f"matched {hit[1]}" if hit else "no matching Ollama tag (nothing is downloaded)")
                info = next((m for m in ollama["models"] if hit and m["name"] == hit[0]), None)
                if info:
                    row.update(size_mb=info.get("size_mb"), capabilities=info.get("capabilities"),
                               context_length=info.get("context_length"), quantization=info.get("quantization"))
        elif e["backend"] == "comfyui":
            if comfyui.get("status") != "ok":
                row.update(status="unknown", detail=f"ComfyUI {comfyui.get('status')}")
            else:
                files = [f for fld in e.get("folders", []) for f in comfyui["folders"].get(fld, [])]
                hit = _files_match(files, e.get("files", []))
                missing = []
                for req in e.get("requires", []):
                    if not _files_match(comfyui["folders"].get(req["folder"], []), req["files"]):
                        missing.append(f"{req['folder']}/{' or '.join(req['files'])}")
                if not hit:
                    row.update(status="missing", detail="model file not found in ComfyUI model folders")
                elif missing:
                    row.update(status="incomplete", files=hit, detail="missing: " + "; ".join(missing))
                else:
                    row.update(status="installed", files=hit, detail="file found (runtime not tested here)")
                row["workflow_available"] = e.get("workflow") in comfyui.get("workflows", [])
        else:
            pr = probes.get(e["backend"]) or {}
            st = pr.get("status")
            row.update(status="installed" if st == "ok" else ("unknown" if st is None else "missing"),
                       detail="; ".join(pr.get("problems", [])) or pr.get("error") or st or "no probe")
        key = (row.get("installed_id") or "").lower()
        row["registered"] = bool(key and key in used) or any(p["id"] == e["id"] for p in registry_presets)
        out.append(row)
    return out


def report(s: dict | None = None) -> dict:
    from shams.models import ModelRegistry, discover_ollama
    s = s or _settings()
    o = (s.get("models") or {}).get("ollama") or {}
    oll = discover_ollama(o.get("base_url"), fresh=True)
    reg = ModelRegistry(s, ollama=oll)
    rep = reg.report()
    presets = []
    for p in reg.presets():
        p = dict(p)
        p.pop("worker", None)
        presets.append(p)
    cu = discover_comfyui(s)
    raw = {pid: pc for pid, pc in ((s.get("models") or {}).get("presets") or {}).items()} \
        if isinstance((s.get("models") or {}).get("presets"), dict) else {}
    return {"ollama": rep["ollama"], "comfyui": cu, "roles": rep["roles"], "tools": rep["tools"], "presets": presets,
            "preset_config": raw, "inventory": inventory(oll, cu, presets, rep["tools"]),
            "allow_adult_content": bool((s.get("general") or {}).get("allow_adult_content", False))}


# --------------------------------------------------------------------------- registry edits (user settings)
def _user_presets() -> dict:
    user = settings_mod.read_scope("user", None)
    presets = ((user.get("models") or {}).get("presets"))
    if isinstance(presets, list):
        presets = {str(p["id"]): {k: v for k, v in p.items() if k != "id"} for p in presets if p.get("id")}
    return dict(presets or {})


def _save_user_presets(presets: dict) -> None:
    user = settings_mod.read_scope("user", None)
    models = dict(user.get("models") or {})
    models["presets"] = presets
    user["models"] = models
    settings_mod.save("user", user, None)


def upsert_preset(pid: str, cfg: dict) -> dict:
    if not pid or not all(c.isalnum() or c in "-_." for c in pid):
        raise ValueError("preset id: letters, digits, - _ . only")
    cfg = {k: v for k, v in cfg.items() if v is not None}
    presets = _user_presets()
    base = dict(presets.get(pid) or {})
    base.update(cfg)
    if (base.get("content_policy") or {}).get("adult"):
        base["execution_modes"] = ["direct"]          # adult presets are direct-only (ADR-121)
        base["autonomous_default"] = False
    base.setdefault("execution_modes", ["direct"])
    base.setdefault("autonomous_default", False)      # specialty / manual presets stay out of automatic workflows
    presets[pid] = base
    _save_user_presets(presets)
    return base


def set_enabled(pid: str, enabled: bool) -> dict:
    presets = _user_presets()
    if pid not in presets:
        raise KeyError(f"preset {pid!r} is not in the user settings")
    presets[pid]["enabled"] = bool(enabled)
    _save_user_presets(presets)
    return presets[pid]


def remove_preset(pid: str) -> None:
    presets = _user_presets()
    if pid not in presets:
        raise KeyError(f"preset {pid!r} is not in the user settings")
    del presets[pid]
    _save_user_presets(presets)


def suggest_preset(entry_id: str, rep: dict) -> tuple[str, dict]:
    """Preset config for an installed inventory entry (used by "Register")."""
    row = next((r for r in rep["inventory"] if r["id"] == entry_id), None)
    if row is None:
        raise KeyError(f"no inventory entry {entry_id!r}")
    if row["status"] not in ("installed",):
        raise ValueError(f"{row['label']} is {row['status']}: {row.get('detail')}")
    cfg: dict[str, Any] = {"label": row["label"], "kind": row["kind"], "execution_modes": ["direct"],
                           "autonomous_default": False}
    if row["backend"] == "ollama":
        cfg.update(provider="ollama", model=row["installed_id"])
    elif row["backend"] == "comfyui":
        cfg.update(provider="comfyui", workflow=row["workflow"])
        files = row.get("files") or []
        if row["workflow"] == "sdxl_checkpoint" and files:
            cfg["params"] = {"checkpoint": files[0]}
        if row["workflow"] == "wan_t2v_1_3b" and files:
            cfg["params"] = {"unet": files[0]}
        if row.get("adult"):
            cfg["content_policy"] = {"adult": True}
    else:
        cfg.update(provider=row["backend"])
    return entry_id, cfg


# --------------------------------------------------------------------------- lifecycle
def ollama_load(model: str, keep_alive: Any = "10m") -> dict:
    p = ollama_provider()
    p.load_with(model, keep_alive)
    return {"loaded": p.ps()}


def ollama_unload(model: str | None = None) -> dict:
    p = ollama_provider()
    names = [model] if model else [m["name"] for m in p.ps()]
    for n in names:
        p.unload(n)
    return {"unloaded": names, "loaded": p.ps()}


def comfy_free() -> dict:
    cu = comfy()
    if cu is None:
        raise RuntimeError("ComfyUI is not configured (tools.comfyui)")
    cu.unload()
    return {"freed": True}


def comfy_start() -> dict:
    cu = comfy()
    if cu is None:
        raise RuntimeError("ComfyUI is not configured (tools.comfyui)")
    if cu.health() == "ok":
        return {"status": "ok", "started": False}
    cu.autostart = True
    cu.ensure_running()
    return {"status": cu.health(), "started": True}
