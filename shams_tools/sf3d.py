"""Stable Fast 3D provider (ADR-120): single-image -> textured GLB, as a local command.

SF3D is a *specialized* image-to-3D generator. It is never the default 3D path: the 3D router uses
reuse -> primitives -> procedural (Blender Python) -> modular -> placeholder first, and SF3D only when a
reference image exists and the object is genuinely complex (see shams_tools.routing.choose_3d_route).

Runs ``<python> run.py <image> --output-dir <dir> [--pretrained-model <weights>] ...`` inside the SF3D
repository with its own virtual environment; the mesh is ``<dir>/0/mesh.glb``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from shams.providers import ProviderError
from shams.workers import base_handoff, output_plan
from shams_tools.routing import choose_3d_route


class StableFast3D:
    name = "stable_fast_3d"
    supports_tools = False

    def __init__(self, repo: str, python: str | None = None, weights: str | None = None, device: str | None = None,
                 texture_resolution: int = 1024, remesh: str = "none", foreground_ratio: float = 0.85,
                 timeout: float = 900.0, env: dict[str, str] | None = None, **_: Any):
        self.repo = Path(repo)
        self.python = python or self._venv_python()
        self.weights, self.device = weights, device
        self.texture_resolution, self.remesh = int(texture_resolution), remesh
        self.foreground_ratio, self.timeout = float(foreground_ratio), float(timeout)
        self.env = dict(env or {})

    def _venv_python(self) -> str:
        for rel in (".venv/Scripts/python.exe", ".venv/bin/python", "venv/Scripts/python.exe", "venv/bin/python"):
            p = self.repo / rel
            if p.exists():
                return str(p)
        return sys.executable

    def health_report(self) -> dict:
        run = self.repo / "run.py"
        problems = []
        if not run.exists():
            problems.append(f"{run} not found")
        elif "--pretrained-model" not in run.read_text(encoding="utf-8", errors="replace"):
            problems.append("run.py has no --pretrained-model option (unexpected SF3D version)")
        if not Path(self.python).exists():
            problems.append(f"python {self.python} not found")
        if self.weights:
            w = Path(self.weights)
            if not (w / "model.safetensors").exists() or not (w / "config.yaml").exists():
                problems.append(f"weights folder {w} lacks model.safetensors/config.yaml")
        return {"status": "ok" if not problems else "down", "problems": problems, "python": self.python,
                "repo": str(self.repo), "weights": self.weights}

    def health(self) -> str:
        return self.health_report()["status"]

    def supports_target_vertices(self) -> str | None:
        run = self.repo / "run.py"
        text = run.read_text(encoding="utf-8", errors="replace") if run.exists() else ""
        for flag in ("--target_vertex_count", "--target-vertex-count"):
            if flag in text:
                return flag
        return None

    def generate(self, image: str | Path, out_dir: str | Path, *, target_vertices: int | None = None) -> dict:
        """Returns {"glb": path, "seconds", "log"}. Raises ProviderError on failure."""
        rep = self.health_report()
        if rep["problems"]:
            raise ProviderError("Stable Fast 3D not usable: " + "; ".join(rep["problems"]))
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        remesh, extra = self.remesh, []
        flag = self.supports_target_vertices() if target_vertices else None
        if flag:                                  # meet a polygon budget: remesh to a vertex target
            remesh = "triangle" if remesh == "none" else remesh
            extra = [flag, str(int(target_vertices))]
        argv = [self.python, "run.py", str(Path(image).resolve()), "--output-dir", str(out.resolve()),
                "--texture-resolution", str(self.texture_resolution), "--remesh_option", remesh,
                "--foreground-ratio", str(self.foreground_ratio), *extra]
        if self.weights:
            argv += ["--pretrained-model", str(Path(self.weights).resolve())]
        if self.device:
            argv += ["--device", self.device]
        t0 = time.monotonic()
        try:
            p = subprocess.run(argv, cwd=self.repo, capture_output=True, text=True, timeout=self.timeout,
                               encoding="utf-8", errors="replace", env={**os.environ, **self.env})
        except subprocess.TimeoutExpired:
            raise ProviderError(f"Stable Fast 3D timed out after {self.timeout:.0f}s") from None
        log = (p.stdout + "\n" + p.stderr).strip()[-3000:]
        (out / "sf3d_run.log").write_text(" ".join(argv) + f"\nexit {p.returncode}\n\n" + log, encoding="utf-8")
        glb = out / "0" / "mesh.glb"
        if p.returncode != 0 or not glb.exists():
            raise ProviderError(f"Stable Fast 3D failed (exit {p.returncode}): {log[-800:]}")
        return {"glb": str(glb), "seconds": round(time.monotonic() - t0, 2), "log": log, "argv": argv}

    def load(self, model: str) -> None:          # one process per call: nothing stays in VRAM
        return None

    def unload(self, model: str | None = None) -> None:
        return None

    def chat(self, messages: list[dict], *, model: str, **opts: Any) -> dict:
        raise ProviderError("stable_fast_3d is an image-to-3D backend; use the sf3d worker")


class StableFast3DWorker:
    """Image-to-3D for one complex object. Refuses (BLOCKED) without a reference image or when the
    route policy says a cheaper path is enough, so it is never called for basic scenes."""

    def __init__(self, *, entry: dict, attempts_root: Path, provider: Any = None,
                 read_blob: Callable[[str], Path] | None = None, **_: Any):
        cfg = entry.get("provider_config", {})
        self.provider = provider or StableFast3D(**{k: v for k, v in cfg.items()
                                                    if k not in ("blender", "impl", "preview_dir")})
        self.attempts_root, self.read_blob = Path(attempts_root), read_blob
        self.force = bool(cfg.get("force", False))
        self.blender = cfg.get("blender")
        # SF3D meshes face the other way from Blender's front: the default camera saw their back
        self.preview_dir = list(cfg.get("preview_dir") or (-1.0, 1.2, 0.8))
        self.model = "stable-fast-3d"

    def run(self, ctx: dict, worker_ref: dict) -> tuple[list[dict], dict]:
        adir, plan = output_plan(ctx, self.attempts_root)
        images = [i for i in ctx.get("inputs", []) if str(i.get("media_type", "")).startswith("image/")]
        if not images:
            return [], base_handoff(ctx, worker_ref, "BLOCKED", "no reference image",
                                    blocked_reason="policy: Stable Fast 3D needs a reference image input")
        explicit = ctx["task"].get("capability") == "mesh.image_to_3d"     # the plan asked for image-to-3D
        route, why = ("image_to_3d", "plan step: mesh from the concept image") if explicit else \
            choose_3d_route(ctx["task"], ctx["inputs"], sf3d_available=True)
        if route != "image_to_3d" and not self.force:
            return [], base_handoff(ctx, worker_ref, "BLOCKED", f"route {route}: {why}",
                                    blocked_reason=f"policy: {why}; use the 3D Scene worker ({route})")
        img = Path(self.read_blob(images[0]["sha256"])) if self.read_blob else Path(images[0]["path"])
        src = adir / "_sf3d" / ("input" + (".png" if images[0]["media_type"] == "image/png" else ".jpg"))
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(img, src)
        budget = next((int(c["check"]["max_triangles"]) for c in ctx["task"].get("acceptance_criteria", [])
                       if (c.get("check") or {}).get("max_triangles")), None)
        kw = {"target_vertices": int(budget * 0.45)} if budget else {}
        try:
            try:
                r = self.provider.generate(src, adir / "_sf3d", **kw)
            except TypeError:                     # a provider without vertex targets
                if not kw:
                    raise
                r = self.provider.generate(src, adir / "_sf3d")
        except ProviderError as e:
            return [], base_handoff(ctx, worker_ref, "FAILED", "Stable Fast 3D failed",
                                    known_issues=[{"issue": str(e)[-800:], "severity": "high"}],
                                    blocked_reason=str(e)[:500])
        glb_out = next((o for o in plan if o["media_type"] == "model/gltf-binary"), None)
        png_out = next((o for o in plan if o["media_type"] == "image/png"), None)
        files, issues, decisions = [], [], [{"decision": "3D route: image_to_3d", "reason": why}]
        if r.get("argv"):
            decisions.append({"decision": "Stable Fast 3D command", "reason": " ".join(map(str, r["argv"][1:]))[:600]})
        height = (ctx["task"].get("params") or {}).get("height_m")
        if self.blender and (png_out or height):
            # SF3D meshes come out unit-sized: scale to real size, frame a preview render, export again
            from shams_tools.blender import run_blender
            work = adir / "_post"
            work.mkdir(parents=True, exist_ok=True)
            spec = {"import_glb": str(r["glb"]), "fit_height": float(height) if height else None,
                    "glb": glb_out["abs"] if glb_out else None, "render": png_out["abs"] if png_out else None,
                    "resolution": 512, "camera_dir": self.preview_dir}
            for o in (glb_out, png_out):
                if o:
                    Path(o["abs"]).parent.mkdir(parents=True, exist_ok=True)
            rep = run_blender(self.blender, spec, work, 600)
            if rep.get("ok"):
                files = [o for o in (glb_out, png_out) if o and Path(o["abs"]).exists()]
                if height:
                    decisions.append({"decision": f"scaled to {height} m tall", "reason": "real-world size"})
            else:
                issues.append({"issue": "post-processing in Blender failed: " + rep.get("error", "")[-500:],
                               "severity": "medium"})
        if glb_out and glb_out not in files:      # no Blender (or it failed): the raw mesh, unscaled
            Path(glb_out["abs"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(r["glb"], glb_out["abs"])
            files.append(glb_out)
            if png_out:
                issues.append({"issue": "no preview render: Blender is needed to render the mesh", "severity": "medium"})
        return files, base_handoff(ctx, worker_ref, "READY_FOR_QC", "Stable Fast 3D mesh from the reference image",
                                   usage={"wall_seconds": r["seconds"]}, decisions=decisions,
                                   known_issues=issues or None)


