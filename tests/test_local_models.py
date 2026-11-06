"""Local models by role, media tools and asset QC (ADR-120). Everything here runs against fakes; the real
machine is exercised by `shams selftest` (shams_tools/selftest.py)."""
import json
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from shams import models, providers
from shams.orchestration.project import Shams
from shams.planning import llm_planner
from shams.verification import assets
from shams.verification.checkers import AssetCheckers, make_visual_checker
from shams_tools import blender as blender_tools
from shams_tools.comfyui import ComfyUI, ComfyUIImageWorker, MissingResources, fill
from shams_tools.routing import choose_3d_route
from shams_tools.sf3d import StableFast3D, StableFast3DWorker

INSTALLED = ["qwen3.5:9b", "qwen2.5-coder:14b", "hf.co/ccore/Qwen3.5-4B-3D-Coder:Q8_0", "qwen3-vl:4b",
             "qwen3-embedding:0.6b"]
CAPS = {"qwen3.5:9b": ["completion", "vision", "tools", "thinking"], "qwen2.5-coder:14b": ["completion", "tools", "insert"],
        "hf.co/ccore/Qwen3.5-4B-3D-Coder:Q8_0": ["tools", "thinking", "completion", "vision"],
        "qwen3-vl:4b": ["completion", "vision", "tools", "thinking"], "qwen3-embedding:0.6b": ["embedding"]}
SIZES = {"qwen3.5:9b": 6550, "qwen2.5-coder:14b": 8990, "hf.co/ccore/Qwen3.5-4B-3D-Coder:Q8_0": 5160,
         "qwen3-vl:4b": 3340, "qwen3-embedding:0.6b": 640}
ROLES = {"planner": {"model": "qwen3.5:9b"}, "coder": {"model": "qwen2.5-coder:14b"},
         "scene3d": {"model": "qwen3.5-4b-coder:Q8"}, "visual_qc": {"model": "qwen3-vl:4b"},
         "embedding": {"model": "qwen3-embedding:0.6b"}}


# --------------------------------------------------------------------------- helpers
def make_glb(path: Path, *, uv=True, scale=1.0, truncate=False, nodes=None) -> Path:
    pos = struct.pack("<9f", 0, 0, 0, scale, 0, 0, 0, scale, 0)
    uvs = struct.pack("<6f", 0, 0, 1, 0, 0, 1)
    binary = pos + (uvs if uv else b"")
    attrs = {"POSITION": 0, **({"TEXCOORD_0": 1} if uv else {})}
    gltf = {"asset": {"version": "2.0", "generator": "test"}, "buffers": [{"byteLength": len(binary)}],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36}] +
                           ([{"buffer": 0, "byteOffset": 36, "byteLength": 24}] if uv else []),
            "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                           "min": [0, 0, 0], "max": [scale, scale, 0]}] +
                         ([{"bufferView": 1, "componentType": 5126, "count": 3, "type": "VEC2"}] if uv else []),
            "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}], "textures": [{"source": 0}],
            "images": [{"uri": "x.png"}],
            "meshes": [{"primitives": [{"attributes": attrs, "material": 0}]}], "nodes": nodes or [{"mesh": 0}]}
    js = json.dumps(gltf).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    binary += b"\0" * ((4 - len(binary) % 4) % 4)
    body = struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(binary), 0x004E4942) + binary
    data = struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body
    path.write_bytes(data[:-10] if truncate else data)
    return path


def make_png(path: Path, w=64, h=64, blank=False) -> Path:
    rows = b""
    for y in range(h):
        rows += b"\x00" + b"".join(bytes(((0 if blank else (x * 4) % 256), (0 if blank else (y * 4) % 256), 128))
                                   for x in range(w))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return path


class Server:
    def __init__(self, handler):
        outer = self
        self.calls = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, ctype="application/json"):
                b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                outer.calls.append(("GET", self.path, None))
                self._send(*handler("GET", self.path, None))

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                outer.calls.append(("POST", self.path, body))
                self._send(*handler("POST", self.path, body))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


class FakeOllama:
    def __init__(self, reply=lambda body: "OK"):
        self.loaded: list[str] = []
        self.reply = reply
        self.server = Server(self.handle)
        self.url = self.server.url

    def handle(self, method, path, body):
        if path == "/api/version":
            return 200, {"version": "0.40.14"}
        if path == "/api/tags":
            return 200, {"models": [{"name": n, "size": SIZES[n] * 2**20, "details": {"family": "qwen"}} for n in INSTALLED]}
        if path == "/api/ps":
            return 200, {"models": [{"name": n, "size": SIZES[n] * 2**20, "size_vram": SIZES[n] * 2**20}
                                    for n in self.loaded]}
        if path == "/api/show":
            return 200, {"capabilities": CAPS[body["model"]], "model_info": {"qwen.context_length": 32768}}
        if path == "/api/generate":
            if body.get("keep_alive") == 0:
                self.loaded = [m for m in self.loaded if m != body["model"]]
            elif body["model"] not in self.loaded:
                self.loaded.append(body["model"])
            return 200, {"done": True}
        if path == "/api/chat":
            if body["model"] not in self.loaded:
                self.loaded.append(body["model"])
            return 200, {"message": {"role": "assistant", "content": self.reply(body)}, "prompt_eval_count": 5,
                         "eval_count": 3}
        return 404, {"error": "not found"}

    def close(self):
        self.server.close()


@pytest.fixture
def ollama():
    o = FakeOllama()
    models._CACHE.clear()
    yield o
    o.close()
    models._CACHE.clear()


# --------------------------------------------------------------------------- registry
@pytest.mark.parametrize("wanted,expected", [
    ("qwen3.5-4b-coder:Q8", "hf.co/ccore/Qwen3.5-4B-3D-Coder:Q8_0"),
    ("qwen3.5:9b", "qwen3.5:9b"), ("qwen2.5-coder:14b", "qwen2.5-coder:14b"),
    ("QWEN3-VL:4B", "qwen3-vl:4b"), ("qwen3-embedding", "qwen3-embedding:0.6b"), ("mimo-v2.6", None),
])
def test_match_model_maps_preferred_names_onto_installed_ids(wanted, expected):
    got, how = models.match_model(wanted, INSTALLED)
    assert got == expected, how


def test_match_model_reports_ambiguity_instead_of_guessing():
    got, how = models.match_model("qwen:4b", ["qwen-a:4b", "qwen-b:4b"])
    assert got is None and how.startswith("ambiguous")
    assert models.match_model("x", ["y:1"], {"x": "y:1"}) == ("y:1", "alias")


def test_registry_binds_roles_and_generates_workers(ollama):
    s = {"models": {"ollama": {"base_url": ollama.url}, "roles": ROLES}}
    reg = models.ModelRegistry(s)
    by = {b.role: b for b in reg.bindings()}
    assert all(b.ok for r, b in by.items() if r != "build"), [b.as_dict() for b in by.values()]
    assert by["scene3d"].model_id == "hf.co/ccore/Qwen3.5-4B-3D-Coder:Q8_0" and by["scene3d"].how == "fuzzy"
    ws = {w["id"]: w for w in reg.workers()}
    assert "worker.role.embedding" not in ws                     # retrieval role: no scheduler worker
    coder = ws["worker.role.coder"]
    assert coder["provider"] == "ollama" and coder["model"] == "qwen2.5-coder:14b" and "code.*" in coder["capabilities"]
    assert 9000 < coder["vram_mb"] < 12288 and coder["config"]["num_ctx"] == 16384
    assert ws["worker.role.scene3d"]["config"]["impl"] == "scene3d"
    assert not any("qwen" in str(x) for x in models.ROLE_SPECS.values())     # no model id in code


def test_registry_reports_missing_and_down():
    s = {"models": {"ollama": {"base_url": "http://127.0.0.1:9"}, "roles": {"coder": {"model": "x"}}}}
    models._CACHE.clear()
    b = models.ModelRegistry(s).binding("coder")
    assert b.status == "down" and not [w for w in models.ModelRegistry(s).workers() if w["id"] != "worker.role.build"]
    s2 = {"models": {"roles": {"image": {}}}}
    assert models.ModelRegistry(s2).binding("image").status == "not_configured"


def test_shams_uses_role_workers_and_wildcard_capabilities(tmp_path, ollama, isolated_home):
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "settings.json").write_text(json.dumps(
        {"models": {"ollama": {"base_url": ollama.url}, "roles": ROLES}}), encoding="utf-8")
    d = tmp_path / "p"
    Shams.init_project(d, "lm", "tool", mode="autonomous", domain="software", domain_config={"stack": "python"})
    st = Shams(d)
    try:
        assert [w.worker_id for w in st.nodes.candidates("code.python")] == ["worker.role.coder"]
        assert [w.worker_id for w in st.nodes.candidates("scene.basic")] == ["worker.role.scene3d"]
        assert st.nodes.candidates("plan.dag")[0].model == "qwen3.5:9b"
        assert st.nodes.workers["worker.role.coder"].entry["runtime"]["load_adapter"] == "ollama"
    finally:
        st.close()


# --------------------------------------------------------------------------- ollama provider
def test_ollama_native_chat_unload_and_resources(ollama):
    p = providers.create("ollama", {"base_url": ollama.url, "num_ctx": 8192})
    r = p.chat([{"role": "user", "content": "hi"}], model="qwen3-vl:4b", images=["aGk="], format="json")
    body = [c[2] for c in ollama.server.calls if c[1] == "/api/chat"][-1]
    assert r["text"] == "OK" and body["think"] is False and body["options"]["num_ctx"] == 8192
    assert body["messages"][-1]["images"] == ["aGk="] and body["format"] == "json"
    assert p.resources()["loaded"][0]["name"] == "qwen3-vl:4b"
    p.unload("qwen3-vl:4b")
    assert p.ps() == []
    d = models.discover_ollama(ollama.url, fresh=True)
    assert d["status"] == "ok" and d["version"] == "0.40.14" and len(d["models"]) == 5
    assert next(m for m in d["models"] if m["name"] == "qwen3-vl:4b")["capabilities"][1] == "vision"


# --------------------------------------------------------------------------- planner
def test_llm_planner_repairs_an_invalid_plan(ollama):
    good = {"tasks": [
        {"task_id": "Write module", "title": "module", "capability": "code.python", "depends_on": [],
         "outputs": [{"name": "m", "logical_id": "code.m", "media_type": "text/x-python"}],
         "acceptance_criteria": [{"description": "compiles", "kind": "compile"}]},
        {"task_id": "task.tests", "title": "tests", "capability": "code.python", "depends_on": ["Write module"],
         "inputs": [{"name": "m", "from_task": "Write module", "logical_id": "code.m"}],
         "outputs": [{"name": "t", "logical_id": "test.m", "media_type": "text/x-python"}],
         "acceptance_criteria": ["tests pass"]}]}
    bad = {"tasks": [{**good["tasks"][1], "depends_on": []}]}
    replies = iter([json.dumps(bad), json.dumps(good)])

    class P:
        name = "fake"

        def chat(self, messages, model, **kw):
            return {"text": next(replies)}
    r = llm_planner.plan(P(), "m", goal="g", project_id="pp", capabilities=["code.*"])
    assert r["dag"] is not None and r["rounds"] == 2, r["issues"]
    t = r["dag"]["tasks"]
    assert t[0]["task_id"] == "task.write_module" and t[1]["depends_on"] == ["task.write_module"]
    assert t[1]["acceptance_criteria"][0]["kind"] == "semantic"


def test_plan_from_llm_falls_back_to_template_without_planner(tmp_path):
    d = tmp_path / "p"
    Shams.init_project(d, "fb", "tool", mode="autonomous", mock_mode=True, domain="software",
                       domain_config={"stack": "python"})
    st = Shams(d)
    try:
        dag = st.plan_from_llm()
        assert dag["template"].startswith("software_basic")
        ev = st.db.q1("SELECT payload_json FROM events WHERE type='plan.llm_attempted'")[0]
        assert '"ok": false' in ev
    finally:
        st.close()


# --------------------------------------------------------------------------- assets
def test_glb_and_png_inspection(tmp_path):
    g = make_glb(tmp_path / "a.glb", scale=2.0)
    info = assets.parse_glb(g)
    assert info["vertices"] == 3 and info["triangles"] == 1 and info["has_uv"] and info["textured"]
    assert assets.check_glb(g, {"require_uv": True, "require_texture": True, "max_size_m": 5})[0] == "PASS"
    assert assets.check_glb(g, {"max_size_m": 1})[0] == "FAIL"
    assert assets.check_glb(make_glb(tmp_path / "b.glb", uv=False), {"require_uv": True})[0] == "FAIL"
    v, d = assets.check_glb(make_glb(tmp_path / "c.glb", truncate=True))
    assert v == "FAIL" and "GLB invalid" in d
    assert assets.check_png(make_png(tmp_path / "ok.png"))[0] == "PASS"
    v, d = assets.check_png(make_png(tmp_path / "blank.png", blank=True))
    assert v == "FAIL" and "blank" in d


def test_png_check_rejects_random_noise(tmp_path):
    import random
    rnd, w, h = random.Random(1), 96, 96
    rows = b"".join(b"\x00" + bytes(rnd.randrange(256) for _ in range(w * 3)) for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    noise = tmp_path / "noise.png"
    noise.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                      + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    v, d = assets.check_png(noise, {"min_std": 8})
    assert v == "FAIL" and "random noise" in d
    assert assets.png_info(make_png(tmp_path / "grad.png"))["roughness"] < 0.2



def test_png_check_keeps_low_contrast_fine_textures(tmp_path):
    import random
    rnd, w, h = random.Random(2), 128, 128
    grid = [[rnd.randrange(108, 149) for _ in range(w // 2)] for _ in range(h // 2)]   # fine soil-like grain
    rows = b"".join(b"\x00" + bytes(v for x in range(w) for v in (grid[y // 2][x // 2],) * 3) for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    tex = tmp_path / "soil.png"
    tex.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    i = assets.png_info(tex)
    assert i["roughness"] > 0.25 and i["std"] < 15                     # the ratio alone called this noise
    assert assets.check_png(tex, {"power_of_two": True})[0] == "PASS"


def test_glb_width_to_height_catches_several_figures(tmp_path):
    # make_glb is a triangle in the XY plane: width 1, height 1 (glTF Y-up)
    assert assets.check_glb(make_glb(tmp_path / "a.glb"), {"max_width_to_height": 1.2})[0] == "PASS"
    v, d = assets.check_glb(make_glb(tmp_path / "b.glb"), {"max_width_to_height": 0.9})
    assert v == "FAIL" and "side by side" in d


def test_comfy_worker_puts_text_brief_first_and_vlm_sees_it(tmp_path):
    seen = {}
    class Prov:
        def generate(self, workflow, params):
            seen.update(params)
            return {"images": [make_png(tmp_path / "o.png").read_bytes()], "seconds": 1, "params": {}}
    brief = tmp_path / "brief.txt"
    brief.write_text("adult woman, long black hair, green summer dress\n", encoding="utf-8")
    w = ComfyUIImageWorker(entry={"worker": {"worker_id": "w"}, "provider_config": {}}, attempts_root=tmp_path,
                           provider=Prov(), read_blob=lambda sha: brief)
    ctx = {"project_id": "p", "attempt": 1, "goal": "một yêu cầu tiếng Việt",
           "inputs": [{"logical_id": "brief.npc", "media_type": "text/plain", "sha256": "x"}],
           "task": {"task_id": "task.c", "title": "c", "description": "single full-body image", "acceptance_criteria": [],
                    "capability": "image.concept",
                    "outputs": [{"name": "c", "logical_id": "img.c", "media_type": "image/png"}]}}
    files, h = w.run(ctx, {"worker_id": "w", "kind": "tool"})
    assert h["status"] == "READY_FOR_QC"
    assert seen["prompt"].startswith("adult woman, long black hair") and "tiếng Việt" not in seen["prompt"]
    got = {}
    chk = AssetCheckers(visual=lambda files, desc, c: got.setdefault("d", desc) and ("PASS", "ok")).table()["visual"]
    outs = [{"logical_id": "img.c", "media_type": "image/png", "file": files[0]["abs"], "role": "output"},
            {"logical_id": "brief.npc", "media_type": "text/plain", "file": str(brief), "role": "input"}]
    chk("visual", {"id": "v", "kind": "visual", "description": "one person", "check": {"type": "vlm"}}, [], outputs=outs)
    assert "green summer dress" in got["d"]
    brief.write_text("A woman in her 30s, red coat. Rendered in low-poly style, garden background.", encoding="utf-8")
    ctx["task"]["params"] = {"brief_skip": ["background", "rendered"]}
    w.run(ctx, {"worker_id": "w", "kind": "tool"})
    assert seen["prompt"].startswith("A woman in her 30s, red coat.") and "garden" not in seen["prompt"]
    assert "brief_skip" not in seen
    brief.write_text("A man in his 30s. Short dark hair, jeans. Comfortable for a garden date. Third sentence.", encoding="utf-8")
    ctx["task"]["params"] = {"brief_skip": ["garden"], "brief_max_sentences": 2, "prompt_prefix": "plain white background."}
    w.run(ctx, {"worker_id": "w", "kind": "tool"})
    assert seen["prompt"].startswith("plain white background. A man in his 30s. Short dark hair, jeans.")
    assert "garden" not in seen["prompt"] and "Third" not in seen["prompt"] and "prompt_prefix" not in seen
    brief.write_text("A man in his late 20s with hair styled neatly. Rendered in a garden style.", encoding="utf-8")
    ctx["task"]["params"] = {"brief_skip": ["style", "garden"]}
    w.run(ctx, {"worker_id": "w", "kind": "tool"})
    assert seen["prompt"].startswith("A man in his late 20s with hair styled neatly.")    # whole words only


def test_visual_checker_frees_the_vision_model():
    calls = []
    class P:
        def ps(self): return [{"name": "coder"}]
        def unload(self, m): calls.append(m)
        def chat(self, msgs, **kw): return {"text": '{"pass": true, "score": 8, "reason": "ok"}'}
    import tempfile
    img = make_png(Path(tempfile.mkdtemp()) / "a.png")
    assert make_visual_checker(P(), "vl")([str(img)], "x", {})[0] == "PASS"
    assert calls == ["coder", "vl"]

def test_asset_checkers_dispatch_on_check_type(tmp_path):
    table = AssetCheckers({"compile": lambda k, c, contents: ("PASS", "pack checker")}).table()
    g = make_glb(tmp_path / "a.glb")
    outs = [{"logical_id": "mesh.a", "media_type": "model/gltf-binary", "file": str(g), "role": "output"}]
    assert table["schema"]("schema", {"description": "", "check": {"type": "glb"}}, [], outputs=outs)[0] == "PASS"
    assert table["compile"]("compile", {"description": ""}, [], outputs=outs) == ("PASS", "pack checker")
    assert table["metric"]("metric", {"description": ""}, [], outputs=outs)[0] == "NOT_VERIFIED"
    (tmp_path / "temp.py").write_text("def c_to_f(c):\n    return c * 9 / 5 + 32\n", encoding="utf-8")
    (tmp_path / "test_temp.py").write_text("from temp import c_to_f\n\ndef test_it():\n    assert c_to_f(100) == 212\n",
                                           encoding="utf-8")
    (tmp_path / "bad.py").write_text("def x(:\n", encoding="utf-8")
    py = [{"logical_id": "code.temp", "path": "src/temp.py", "media_type": "text/x-python",
           "file": str(tmp_path / "temp.py"), "role": "input"},
          {"logical_id": "test.temp", "path": "tests/test_temp.py", "media_type": "text/x-python",
           "file": str(tmp_path / "test_temp.py"), "role": "output"}]
    assert table["unit_test"]("unit_test", {"description": "", "check": {"type": "pytest"}}, [], outputs=py)[0] == "PASS"
    bad = [{"logical_id": "code.bad", "path": "bad.py", "media_type": "text/x-python", "file": str(tmp_path / "bad.py")}]
    v, d = table["compile"]("compile", {"description": "", "check": {"type": "python"}}, [], outputs=bad)
    assert v == "FAIL" and "SyntaxError" in d


def test_visual_checker_uses_the_vision_model(tmp_path):
    seen = {}

    class P:
        def chat(self, messages, model, images=None, format=None, **kw):
            seen.update(model=model, n=len(images), format=format)
            return {"text": '{"pass": true, "score": 8, "reason": "table and red ball visible"}'}
    v, d = make_visual_checker(P(), "qwen3-vl:4b", exclusive=False)([str(make_png(tmp_path / "r.png"))], "table", {})
    assert v == "PASS" and seen == {"model": "qwen3-vl:4b", "n": 1, "format": "json"} and "8/10" in d


# --------------------------------------------------------------------------- 3D route + Stable Fast 3D
IMG = [{"logical_id": "ref", "media_type": "image/png", "sha256": "0" * 64, "path": "ref.png"}]


def test_3d_route_never_picks_image_to_3d_for_basic_scenes():
    basic = {"title": "Living room", "description": "a sofa, a table and a lamp"}
    assert choose_3d_route(basic, [], sf3d_available=True)[0] == "procedural"
    assert choose_3d_route(basic, IMG, sf3d_available=True)[0] != "image_to_3d"
    assert choose_3d_route({"title": "Crate", "description": "a simple box"}, [], sf3d_available=True)[0] == "primitives"
    assert choose_3d_route({"title": "Greybox level"}, [], sf3d_available=True)[0] == "placeholder"
    hero = {"title": "Detailed character statue", "description": "organic sculpt"}
    assert choose_3d_route(hero, IMG, sf3d_available=True)[0] == "image_to_3d"
    assert choose_3d_route(hero, IMG, sf3d_available=False)[0] == "procedural"
    assert choose_3d_route(hero, [{"media_type": "model/gltf-binary"}], sf3d_available=True)[0] == "reuse"


FAKE_SF3D = """
import argparse, os, shutil, sys
p = argparse.ArgumentParser(); p.add_argument("image"); p.add_argument("--output-dir")
p.add_argument("--pretrained-model"); p.add_argument("--texture-resolution"); p.add_argument("--remesh_option")
p.add_argument("--foreground-ratio"); p.add_argument("--device")
a = p.parse_args()
os.makedirs(os.path.join(a.output_dir, "0"), exist_ok=True)
shutil.copyfile(os.environ["FAKE_GLB"], os.path.join(a.output_dir, "0", "mesh.glb"))
"""


def test_stable_fast_3d_provider_and_worker_gating(tmp_path, monkeypatch):
    repo = tmp_path / "sf3d"
    repo.mkdir()
    (repo / "run.py").write_text(FAKE_SF3D, encoding="utf-8")
    monkeypatch.setenv("FAKE_GLB", str(make_glb(tmp_path / "src.glb")))
    prov = StableFast3D(repo=str(repo), python=sys.executable)
    assert prov.health() == "ok"
    assert StableFast3D(repo=str(tmp_path / "none"), python=sys.executable).health() == "down"
    img = make_png(tmp_path / "ref.png")
    w = StableFast3DWorker(entry={"worker": {"worker_id": "w"}, "provider_config": {}}, attempts_root=tmp_path / "att",
                           provider=prov, read_blob=lambda sha: img)
    ref = {"worker_id": "w", "kind": "tool"}
    task = {"task_id": "task.hero", "title": "Detailed character statue", "description": "organic",
            "acceptance_criteria": [],
            "outputs": [{"name": "hero", "logical_id": "mesh.hero", "media_type": "model/gltf-binary"}]}
    ctx = {"project_id": "p", "attempt": 1, "inputs": [], "task": task}
    assert w.run(ctx, ref)[1]["status"] == "BLOCKED"                                       # no image
    assert w.run({**ctx, "task": {**task, "title": "Chair", "description": "simple"}, "inputs": IMG},
                 ref)[1]["status"] == "BLOCKED"                                            # basic -> procedural
    files, h = w.run({**ctx, "inputs": IMG}, ref)
    assert h["status"] == "READY_FOR_QC" and assets.check_glb(files[0]["abs"], {"require_uv": True})[0] == "PASS"


def test_sf3d_plan_step_skips_route_policy_and_meets_budget(tmp_path, monkeypatch):
    repo = tmp_path / "sf3d"
    repo.mkdir()
    fake = FAKE_SF3D.replace('p.add_argument("--device")', 'p.add_argument("--device"); p.add_argument("--target_vertex_count")')
    fake += "\nopen(os.path.join(a.output_dir, 'argv.txt'), 'w').write(' '.join(sys.argv))\n"
    (repo / "run.py").write_text(fake, encoding="utf-8")
    monkeypatch.setenv("FAKE_GLB", str(make_glb(tmp_path / "src.glb")))
    prov = StableFast3D(repo=str(repo), python=sys.executable)
    assert prov.supports_target_vertices() == "--target_vertex_count"
    img = make_png(tmp_path / "ref.png")
    w = StableFast3DWorker(entry={"worker": {"worker_id": "w"}, "provider_config": {}}, attempts_root=tmp_path / "att",
                           provider=prov, read_blob=lambda sha: img)
    assert w.blender is None
    task = {"task_id": "task.npc", "title": "Chair", "description": "simple", "capability": "mesh.image_to_3d",
            "params": {"height_m": 1.7},
            "acceptance_criteria": [{"id": "a", "check": {"type": "glb", "max_triangles": 3000}}],
            "outputs": [{"name": "npc", "logical_id": "mesh.npc", "media_type": "model/gltf-binary"},
                        {"name": "npc_preview", "logical_id": "img.npc", "media_type": "image/png"}]}
    files, h = w.run({"project_id": "p", "attempt": 1, "inputs": IMG, "task": task}, {"worker_id": "w", "kind": "tool"})
    assert h["status"] == "READY_FOR_QC"                         # plan step: no "basic -> procedural" refusal
    argv = next((tmp_path / "att").rglob("argv.txt")).read_text()
    assert "--target_vertex_count 1350" in argv and "--remesh_option triangle" in argv
    assert [f["media_type"] for f in files] == ["model/gltf-binary"]       # no Blender: raw mesh, no preview
    assert any("preview" in i["issue"] for i in h["known_issues"])


def test_game_template_characters_are_built_by_the_3d_model_from_a_brief():
    from shams.planning.validator import instantiate as inst
    dag = inst("vertical_slice_3d", "p", "g", engine="web")
    t = {x["task_id"]: x for x in dag["tasks"]}
    assert not {x for x in t if x.endswith("_concept")} and "task.environment_texture" not in t
    assert not any(x["capability"].startswith(("image.", "mesh.image_to_3d")) for x in dag["tasks"])
    for who in ("player", "npc"):
        m = t[f"task.{who}_model"]
        assert m["capability"] == "mesh.procedural" and m["params"]["builder"] == "json"
        assert m["depends_on"] == ["task.character_brief"] and m["inputs"][0]["logical_id"] == f"brief.{who}"
        assert m["params"]["height_m"] == 1.7 and "1.7 m" in m["description"]
        assert m["acceptance_criteria"][0]["check"]["max_width_to_height"] == 0.9
    e = t["task.environment_model"]
    assert e["capability"] == "mesh.procedural" and e["outputs"][0]["logical_id"] == "asset.environment.mesh"
    integ = t["task.integration_slice1"]
    assert "task.environment_model" in integ["depends_on"]
    assert any(i["logical_id"] == "asset.environment.mesh" for i in integ["inputs"])
    assert "./models/Environment/environment.glb" in integ["description"]
    b = t["task.character_brief"]
    assert b["capability"].startswith("plan.") and {o["media_type"] for o in b["outputs"]} == {"text/plain"}


# --------------------------------------------------------------------------- ComfyUI
class FakeComfy:
    def __init__(self, checkpoints=("sdxl_lightning_4step.safetensors",)):
        self.png = make_png(Path(__import__("tempfile").mkdtemp()) / "out.png").read_bytes()
        self.ckpts = list(checkpoints)
        self.server = Server(self.handle)
        self.url = self.server.url

    def handle(self, method, path, body):
        if path == "/system_stats":
            return 200, {"devices": [{"name": "cuda:0 RTX 3060", "type": "cuda", "vram_total": 12 * 2**30,
                                      "vram_free": 10 * 2**30}]}
        if path.startswith("/object_info/"):
            cls = path.split("/")[-1]
            known = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [self.ckpts]}}},
                     "UNETLoader": {"input": {"required": {"unet_name": ["COMBO", {"options": []}]}}}}
            generic = {"EmptyLatentImage", "CLIPTextEncode", "KSampler", "VAEDecode", "SaveImage", "CLIPLoader",
                       "VAELoader", "EmptySD3LatentImage", "ConditioningZeroOut"}
            if cls in known:
                return 200, {cls: known[cls]}
            return 200, ({cls: {"input": {"required": {}}}} if cls in generic else {})
        if path == "/prompt":
            assert body["prompt"]["3"]["inputs"]["steps"] == 4
            return 200, {"prompt_id": "p1", "node_errors": {}}
        if path.startswith("/history/"):
            return 200, {"p1": {"status": {"status_str": "success", "completed": True},
                                "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}}}}
        if path.startswith("/view"):
            return 200, self.png
        if path == "/free":
            return 200, {}
        return 404, {}

    def close(self):
        self.server.close()


def test_comfyui_generates_and_reports_missing_models(tmp_path):
    fc = FakeComfy()
    try:
        prov = ComfyUI(base_url=fc.url, poll_seconds=0.01)
        assert prov.health() == "ok" and prov.resources()["devices"][0]["vram_free_mb"] == 10240
        r = prov.generate("sdxl_lightning_4step", {"prompt": "cabin"})
        assert r["images"][0][:4] == b"\x89PNG"
        with pytest.raises(MissingResources) as e:
            prov.generate("z_image_turbo", {"prompt": "x"})
        assert any("ModelSamplingAuraFlow" in p for p in e.value.problems)
        assert any("z_image_turbo" in p for p in e.value.problems)
        entry = {"worker": {"worker_id": "w.img", "model": "z_image_turbo"},
                 "provider_config": {"workflow": "z_image_turbo"}}
        w = ComfyUIImageWorker(entry=entry, attempts_root=tmp_path, provider=prov)
        ctx = {"project_id": "p", "attempt": 1, "inputs": [], "goal": "",
               "task": {"task_id": "task.img", "title": "cabin", "acceptance_criteria": [],
                        "outputs": [{"name": "cabin", "logical_id": "image.cabin", "media_type": "image/png"}]}}
        _, h = w.run(ctx, {"worker_id": "w.img", "kind": "tool"})
        assert h["status"] == "BLOCKED" and h["blocked_reason"].startswith("tool unavailable")
        w2 = ComfyUIImageWorker(entry={**entry, "provider_config": {"workflow": "sdxl_lightning_4step"}},
                                attempts_root=tmp_path, provider=prov)
        files, h2 = w2.run(ctx, {"worker_id": "w.img", "kind": "tool"})
        assert h2["status"] == "READY_FOR_QC" and assets.check_png(files[0]["abs"])[0] == "PASS"
    finally:
        fc.close()


def test_comfy_fill_keeps_types():
    g = fill({"a": {"inputs": {"steps": "{{steps}}", "text": "a {{prompt}} b"}}}, {"steps": 4, "prompt": "cat"})
    assert g == {"a": {"inputs": {"steps": 4, "text": "a cat b"}}}


# --------------------------------------------------------------------------- 3D scene worker
FAKE_BLENDER = r'''
import json, sys, shutil, os
spec = json.load(open(sys.argv[sys.argv.index("--") + 1]))
code = open(spec["script"]).read() if spec.get("script") else ""
rep = {"blender": "fake-5.2"}
if "BROKEN" in code:
    rep.update(ok=False, error="Traceback: NameError: name 'cube' is not defined")
else:
    rep.update(ok=True, mesh_objects=2, cameras=1, lights=1, materials=["Wood"],
               objects=[{"name": "Table", "type": "MESH", "triangles": 12, "non_manifold_edges": 0}])
    if spec.get("glb"): shutil.copyfile(os.environ["FAKE_GLB"], spec["glb"])
    if spec.get("render"): shutil.copyfile(os.environ["FAKE_PNG"], spec["render"])
json.dump(rep, open(spec["report"], "w"))
'''


@pytest.fixture
def fake_blender(tmp_path, monkeypatch):
    script = tmp_path / "fake_blender.py"
    script.write_text(FAKE_BLENDER, encoding="utf-8")
    if sys.platform == "win32":
        exe = tmp_path / "blender.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        exe = tmp_path / "blender"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("FAKE_GLB", str(make_glb(tmp_path / "scene_src.glb")))
    monkeypatch.setenv("FAKE_PNG", str(make_png(tmp_path / "render_src.png")))
    return str(exe)


def test_scene3d_worker_fixes_forbidden_and_broken_scripts(tmp_path, fake_blender):
    replies = iter(["```python\nimport os\nimport bpy\n```", "```python\nimport bpy\nBROKEN\n```",
                    "```python\nimport bpy\nbpy.ops.mesh.primitive_cube_add()\n```\nSUMMARY: table"])
    prompts = []

    class P:
        name = "fake"

        def chat(self, messages, model, **kw):
            prompts.append(messages[-1]["content"])
            return {"text": next(replies)}
    entry = {"worker": {"worker_id": "worker.role.scene3d", "model": "3d-coder"},
             "provider_config": {"blender": fake_blender, "max_fix_rounds": 2}}
    w = blender_tools.Scene3DWorker(entry=entry, attempts_root=tmp_path / "att", provider=P())
    task = {"task_id": "task.scene", "title": "Table scene", "description": "a table with a ball",
            "params": {"builder": "blender"},
            "acceptance_criteria": [{"id": "ac.glb", "description": "valid", "kind": "schema", "check": {"type": "glb"}}],
            "outputs": [{"name": "scene", "logical_id": "mesh.scene", "media_type": "model/gltf-binary"},
                        {"name": "render", "logical_id": "image.render", "media_type": "image/png"},
                        {"name": "script", "logical_id": "code.scene", "media_type": "text/x-python"},
                        {"name": "report", "logical_id": "data.scene", "media_type": "application/json"}]}
    files, h = w.run({"project_id": "p", "attempt": 1, "inputs": [], "task": task}, {"worker_id": "w", "kind": "llm_agent"})
    assert h["status"] == "READY_FOR_QC", h
    assert "Forbidden construct" in prompts[1] and "NameError" in prompts[2]
    by = {f["media_type"]: f["abs"] for f in files}
    assert assets.check_glb(by["model/gltf-binary"])[0] == "PASS"
    assert assets.check_png(by["image/png"])[0] == "PASS"
    assert "primitive_cube_add" in Path(by["text/x-python"]).read_text()
    assert h["decisions"][0]["decision"] == "scene builder: blender"
    assert h["decisions"][1]["decision"] == "3D route: procedural"
    ok = AssetCheckers()._scene_report({"check": {"min_objects": 2, "require_camera": True}},
                                       [{"logical_id": "d", "media_type": "application/json", "file": by["application/json"]}])
    assert ok[0] == "PASS"


def test_scene3d_worker_blocks_without_blender(tmp_path, monkeypatch):
    monkeypatch.setattr(blender_tools, "find_blender", lambda configured=None: None)
    w = blender_tools.Scene3DWorker(entry={"worker": {"worker_id": "w"}, "provider_config": {}},
                                    attempts_root=tmp_path, provider=None)
    task = {"task_id": "task.s", "title": "terrain with a boolean-cut cave", "acceptance_criteria": [],
            "outputs": [{"name": "s", "logical_id": "mesh.s", "media_type": "model/gltf-binary"}]}
    _, h = w.run({"project_id": "p", "attempt": 1, "inputs": [], "task": task}, {"worker_id": "w", "kind": "llm_agent"})
    assert h["status"] == "BLOCKED" and "Blender" in h["blocked_reason"]


def test_settings_reject_unknown_role():
    from shams import settings as settings_mod
    errs = settings_mod.validate(settings_mod.merge(settings_mod.DEFAULTS, {"models": {"roles": {"wizard": {}}}}))
    assert any("models.roles.wizard" in e for e in errs)
    example = json.loads((Path(models.__file__).parent / "examples" / "local_models.settings.json").read_text("utf-8"))
    assert not settings_mod.validate(settings_mod.merge(settings_mod.DEFAULTS, example))


def test_selftest_reports_blocked_not_pass_when_nothing_is_installed(tmp_path):
    from shams import settings as settings_mod
    from shams_tools import selftest
    s = settings_mod.merge(settings_mod.DEFAULTS, {"models": {"ollama": {"base_url": "http://127.0.0.1:9"},
                                                              "roles": ROLES}})
    models._CACHE.clear()
    rep = selftest.run(s, tmp_path / "out", log=lambda m: None)
    st = {r["id"]: r["status"] for r in rep["tests"]}
    assert st["6a"] == "PASS" and st["9d"] == "PASS"            # pure policy / input checks, run everywhere
    assert st["4"] == "SKIPPED" and st["8b"] == "SKIPPED" and rep["counts"]["FAIL"] == 0
    assert all(st[t] == "BLOCKED" for t in ("1", "2", "3", "3b", "4b", "5a", "5b", "6b", "7", "8", "9a", "9b", "9c",
                                            "9e")), st
    assert (tmp_path / "out" / "report.md").read_text(encoding="utf-8").count("BLOCKED") >= 9


def test_planner_turns_file_paths_into_ids():
    raw = {"tasks": [
        {"task_id": "a", "title": "a", "capability": "code.python", "depends_on": [],
         "outputs": [{"name": "src/temp.py", "logical_id": "src/temp.py", "media_type": "text/x-python"}],
         "acceptance_criteria": [{"description": "c", "kind": "compile"}]},
        {"task_id": "b", "title": "b", "capability": "code.python", "depends_on": ["a"],
         "inputs": [{"name": "t", "from_task": "a", "logical_id": "src/temp.py"}],
         "outputs": [{"name": "tests/test_temp.py", "logical_id": "tests/test_temp.py", "media_type": "text/x-python"}],
         "acceptance_criteria": [{"description": "c", "kind": "unit_test"}]}]}
    dag = llm_planner.normalize(raw, project_id="pp", goal="g", domain=None)
    from shams.planning.validator import validate_dag
    assert not [i for i in validate_dag(dag) if i.level == "error"]
    assert dag["tasks"][0]["outputs"][0] == {"name": "temp", "logical_id": "src.temp.py", "media_type": "text/x-python"}
    assert dag["tasks"][1]["inputs"][0]["logical_id"] == "src.temp.py"


def test_scene3d_rejects_javascript_before_running_blender(tmp_path, fake_blender):
    replies = iter(["```python\nimport bpy\nfloorGeo = new THREE.PlaneGeometry(14, 14)\n```",
                    "```python\nimport bpy\nbpy.ops.mesh.primitive_cube_add()\n```"])
    prompts = []

    class P:
        def chat(self, messages, model, **kw):
            prompts.append(messages[-1]["content"])
            return {"text": next(replies)}
    entry = {"worker": {"worker_id": "w", "model": "m"}, "provider_config": {"blender": fake_blender}}
    w = blender_tools.Scene3DWorker(entry=entry, attempts_root=tmp_path, provider=P())
    work = tmp_path / "w"
    work.mkdir()
    rep, script, _ = w.build({"task": {"title": "t", "acceptance_criteria": []}}, work,
                             {"glb": str(work / "s.glb"), "render": str(work / "r.png")}, "procedural")
    assert rep["ok"] and "not valid Blender Python" in prompts[1] and "new" in prompts[1]
    assert w.log[0]["result"].startswith("This is not valid Blender Python")


SCENE = {"objects": [{"name": "ground", "shape": "plane", "size": [6, 6], "color": "grass"},
                     {"name": "table_top", "shape": "box", "size": [1.2, 0.1, 0.8], "position": [0, 0.75, 0], "color": "#8b5a2b"},
                     {"name": "ball", "shape": "sphere", "radius": 0.15, "position": [0.2, 0.95, 0], "color": "red"}],
         "lights": [{"type": "sun", "direction": [-0.4, -1, -0.3]}]}


def test_simple_scene_uses_json_builder_without_blender(tmp_path, monkeypatch):
    monkeypatch.setattr(blender_tools, "find_blender", lambda configured=None: None)
    replies = iter(['{"objects": [{"shape": "pyramid"}]}', "Here: " + json.dumps(SCENE)])
    seen = []

    class P:
        name = "ollama"

        def chat(self, messages, model, **kw):
            seen.append((messages[-1]["content"], kw.get("format")))
            return {"text": next(replies), "tokens_in": 10, "tokens_out": 20}
    entry = {"worker": {"worker_id": "w", "model": "3d-coder"}, "provider_config": {"max_fix_rounds": 2}}
    w = blender_tools.Scene3DWorker(entry=entry, attempts_root=tmp_path / "att", provider=P())
    task = {"task_id": "task.scene", "title": "Table with a ball", "description": "a wooden table with a red ball",
            "acceptance_criteria": [{"id": "ac.g", "description": "valid", "kind": "schema", "check": {"type": "glb"}}],
            "outputs": [{"name": "scene", "logical_id": "mesh.scene", "media_type": "model/gltf-binary"},
                        {"name": "render", "logical_id": "image.render", "media_type": "image/png"},
                        {"name": "report", "logical_id": "data.scene", "media_type": "application/json"}]}
    files, h = w.run({"project_id": "p", "attempt": 1, "inputs": [], "task": task}, {"worker_id": "w", "kind": "llm_agent"})
    assert h["status"] == "READY_FOR_QC", h
    assert seen[0][1] == "json" and "pyramid" in seen[1][0]                   # JSON mode + fix round with the error
    by = {f["media_type"]: f["abs"] for f in files}
    assert assets.check_glb(by["model/gltf-binary"], {"require_uv": True, "require_material": True})[0] == "PASS"
    assert assets.check_png(by["image/png"])[0] == "PASS"
    rep = json.loads(Path(by["application/json"]).read_text())
    assert rep["builder"] == "scene_json" and rep["mesh_objects"] == 3 and rep["coverage"] > 0.05
    assert h["decisions"][0]["decision"] == "scene builder: json"
    assert AssetCheckers()._scene_report({"check": {"min_objects": 3, "require_camera": True, "require_light": True}},
                                         [{"logical_id": "d", "media_type": "application/json",
                                           "file": by["application/json"]}])[0] == "PASS"


def test_scene_json_validation_messages_are_actionable(tmp_path):
    from shams_tools import scene_json
    for bad, msg in (({"objects": []}, "non-empty"), ({"objects": [{"shape": "box", "size": [1, 0, 1]}]}, "> 0"),
                     ({"objects": [{"shape": "box", "position": [1, "a", 2]}]}, "position"),
                     ({"objects": [{"shape": "box", "color": "#zz"}]}, "colour"),
                     ({"objects": [{"shape": "box"}], "camera": {"position": [0, 0, 50], "target": [0, 0, 100]}}, "camera")):
        with pytest.raises(scene_json.SceneError, match=msg):
            scene_json.build_scene(bad, None, tmp_path / "r.png")
    rep = scene_json.build_scene(SCENE, tmp_path / "s.glb", None, max_triangles=100000)
    assert rep["mesh_objects"] == 3 and assets.parse_glb(tmp_path / "s.glb")["materials"] == 3
    with pytest.raises(scene_json.SceneError, match="budget"):
        scene_json.build_scene(SCENE, None, None, max_triangles=50)
    from shams_tools.routing import scene_builder
    assert scene_builder({"title": "a red cube on a table"}, blender_available=True)[0] == "json"
    assert scene_builder({"title": "rigged character with animation"}, blender_available=True)[0] == "blender"
    assert scene_builder({"title": "x", "params": {"builder": "blender"}}, blender_available=True)[0] == "blender"


def test_scene_json_fixes_threejs_habits_and_reports_sinking(tmp_path):
    from shams_tools import scene_json
    s = {"objects": [{"name": "floor", "shape": "plane", "size": [10, 10], "rotation": [-90, 0, 0]},
                     {"name": "top", "shape": "box", "size": [1.2, 0.12, 0.8], "position": [0, 0.75, 0]},
                     {"name": "ball", "shape": "sphere", "radius": 0.12, "position": [0, 0.84, 0]}],
         "lights": [{"type": "sun", "direction": [-0.4, 0.8, -0.3]}]}
    with pytest.raises(scene_json.SceneError, match=r"ball sinks 9 cm into top .*y = 0.93"):
        scene_json.build_scene(s, None, None)
    rep = scene_json.build_scene(s, None, None, check_support=False)
    assert rep["bbox"]["min"][1] == 0.0 and rep["bbox"]["max"][1] < 1.0          # floor lies flat
    assert rep["notes"] and "planes are horizontal" in rep["notes"][0]
    s["objects"][2]["position"][1] = 0.93
    assert scene_json.build_scene(s, None, None)["ok"]
    assert scene_json.normalise(s)["lights"][0]["direction"][1] < 0            # upward sun flipped


def test_scene_json_flags_half_sunk_ball_and_wall_below_floor():
    from shams_tools import scene_json
    s = {"objects": [{"name": "floor", "shape": "plane", "size": [20, 20]},
                     {"name": "top", "shape": "box", "size": [1.2, 0.12, 0.8], "position": [0, 0.75, 0]},
                     {"name": "side", "shape": "box", "size": [0.12, 0.75, 0.8], "position": [-0.54, 0.375, 0]},
                     {"name": "ball", "shape": "sphere", "radius": 0.12, "position": [0.15, 0.81, 0.05]},
                     {"name": "wall", "shape": "plane", "size": [12, 6], "position": [0, 3, -5], "rotation": [0, 0, 90]}]}
    with pytest.raises(scene_json.SceneError) as e:
        scene_json.build_scene(s, None, None)
    msg = str(e.value)
    assert "ball sinks 12 cm into top" in msg and "wall goes 3.0 m below the floor" in msg and "top sinks" not in msg
    example = scene_json.SCENE_JSON_PROMPT.split("on it):\n")[1]
    assert scene_json.build_scene(example, None, None)["mesh_objects"] == 7      # the prompt's own example is valid


def test_scene3d_can_use_a_stronger_model_for_blender(ollama):
    roles = {**ROLES, "scene3d": {"model": "qwen3.5-4b-coder:Q8", "blender_model": "qwen2.5-coder:14b"}}
    ws = {w["id"]: w for w in models.ModelRegistry({"models": {"ollama": {"base_url": ollama.url}, "roles": roles}}).workers()}
    cfg = ws["worker.role.scene3d"]["config"]
    assert ws["worker.role.scene3d"]["model"] == "hf.co/ccore/Qwen3.5-4B-3D-Coder:Q8_0"     # JSON scenes
    assert cfg["blender_model"] == "qwen2.5-coder:14b"                                     # Blender scripts
    w = blender_tools.Scene3DWorker(entry={"worker": {"worker_id": "w", "model": "small"},
                                           "provider_config": {"blender_model": "big"}}, attempts_root=Path("."))
    assert (w.model, w.blender_model) == ("small", "big")


def test_scene_json_flags_small_gaps_but_not_intended_floating():
    from shams_tools import scene_json
    legs = [{"name": f"leg{i}", "shape": "box", "size": [0.06, 0.78, 0.06], "position": [x, 0.39, z]}
            for i, (x, z) in enumerate([(0.5, 0.3), (-0.5, 0.3), (0.5, -0.3), (-0.5, -0.3)])]
    s = {"objects": [{"name": "floor", "shape": "plane", "size": [8, 8]}, *legs,
                     {"name": "top", "shape": "box", "size": [1.2, 0.06, 0.8], "position": [0, 0.84, 0]},
                     {"name": "lamp", "shape": "sphere", "radius": 0.1, "position": [0, 2.5, 0]}]}
    with pytest.raises(scene_json.SceneError, match=r"top floats 3 cm above .*y = 0.81"):
        scene_json.build_scene(s, None, None)
    rep = scene_json.build_scene(s, None, None, check_support=False)
    assert len(rep["warnings"]) == 1 and "lamp" not in rep["warnings"][0]
    s["objects"][5]["position"][1] = 0.81
    assert scene_json.build_scene(s, None, None)["warnings"] == []


def test_blender_scenes_get_the_same_physical_checks():
    rep = {"objects": [
        {"name": "Floor", "type": "MESH", "bbox": [[-5, -5, 0], [5, 5, 0]]},
        {"name": "Top", "type": "MESH", "bbox": [[-0.6, -0.4, 0.72], [0.6, 0.4, 0.78]]},
        {"name": "Leg", "type": "MESH", "bbox": [[0.5, 0.3, -0.25], [0.56, 0.36, 0.5]]},
        {"name": "Ball", "type": "MESH", "bbox": [[0.0, 0.0, 0.70], [0.24, 0.24, 0.94]]},
        {"name": "Camera", "type": "CAMERA"}]}
    issues = blender_tools.blender_physics(rep)
    text = "; ".join(issues)
    assert "Leg goes 0.25 m below the floor" in text and "Ball sinks 8 cm into Top" in text and "z=" in text


def test_asset_task_gets_goal_only_as_background():
    from shams_tools.blender import scoped_payload
    p = scoped_payload({}, {"capability": "mesh.text_to_3d"}, {"goal": "garden game with 5 characters", "task": "player"})
    assert "goal" not in p and p["project_background"].startswith("garden") and "ONLY" in p["scope"]
    q = scoped_payload({}, {"capability": "scene.build"}, {"goal": "a table scene", "task": "scene"})
    assert q["goal"] == "a table scene" and "scope" not in q


def test_png_power_of_two_and_max_size(tmp_path):
    assert assets.check_png(make_png(tmp_path / "a.png", 64, 64), {"power_of_two": True})[0] == "PASS"
    v, d = assets.check_png(make_png(tmp_path / "b.png", 96, 64), {"power_of_two": True})
    assert v == "FAIL" and "power of two" in d
    assert assets.check_png(make_png(tmp_path / "c.png", 64, 64), {"max_width": 32})[0] == "FAIL"


def test_ollama_tool_calls_use_native_chat_with_num_ctx():
    """Tool turns must keep num_ctx: the OpenAI-compatible endpoint would run with Ollama's small default."""
    def handle(method, path, body):
        if path == "/api/chat" and body.get("tools"):
            return 200, {"message": {"role": "assistant", "content": "",
                                     "tool_calls": [{"function": {"name": "research__web_search",
                                                                  "arguments": {"query": "three.js"}}}]},
                         "prompt_eval_count": 7, "eval_count": 3}
        return 200, {"message": {"role": "assistant", "content": '{"done": true}'}}
    srv = Server(handle)
    try:
        prov = providers.create("ollama", {"base_url": srv.url, "num_ctx": 16384})
        r = prov.chat_tools([{"role": "user", "content": "x"}], model="m",
                            tools=[{"name": "research__web_search", "input_schema": {"type": "object"}}])
        method, path, body = srv.calls[-1]
        assert path == "/api/chat" and body["options"]["num_ctx"] == 16384 and body["stream"] is False
        assert r["tool_calls"][0]["name"] == "research__web_search" and r["tool_calls"][0]["arguments"]["query"] == "three.js"
        msgs = prov.tool_results([(r["tool_calls"][0], "result")])
        assert msgs[0]["role"] == "tool" and msgs[0]["tool_name"] == "research__web_search"
        r2 = prov.chat_tools([{"role": "user", "content": "x"}], model="m", tools=[])
        assert r2["text"] == '{"done": true}' and not r2["tool_calls"]
    finally:
        srv.close()


def test_llm_worker_reply_without_declared_outputs_is_a_task_failure(tmp_path):
    from shams.workers import LLMWorker

    class P:
        name = "fake"
        def chat(self, messages, *, model, **o):
            return {"text": json.dumps({"files": [{"logical_id": "wrong.id", "content": "x"}], "summary": "s",
                                        "status": "READY_FOR_QC"})}
    ctx = {"attempt": 1, "inputs": [], "project_id": "p", "goal": "g",
           "task": {"task_id": "task.a", "title": "A", "capability": "code.x", "acceptance_criteria": [],
                    "outputs": [{"name": "a", "logical_id": "code.a", "media_type": "text/x-python"}]}}
    w = LLMWorker(entry={"worker": {"model": "m"}}, attempts_root=tmp_path, provider=P())
    ref = {"worker_id": "worker.x", "model": "m"}
    files, h = w.run(ctx, ref)
    assert files == [] and h["status"] == "FAILED" and "wrong.id" in h["blocked_reason"]
    assert (tmp_path / "task.a" / "a1" / "_model_reply.txt").read_text(encoding="utf-8").startswith("{")


def test_tool_call_written_as_text_is_executed():
    from shams.workers import text_tool_call
    names = {"research__web_search"}
    c = text_tool_call('{\n "name": "research__web_search",\n "arguments": {"query": "three.js"}\n}', names)
    assert c["name"] == "research__web_search" and c["arguments"] == {"query": "three.js"}
    assert text_tool_call('{"function": {"name": "research__web_search", "arguments": "{\\"query\\": \\"x\\"}"}}',
                          names)["arguments"] == {"query": "x"}
    assert text_tool_call('{"files": [], "summary": "s", "name": "research__web_search"}', names) is None
    assert text_tool_call('{"name": "other", "arguments": {}}', names) is None
    assert text_tool_call("plain words", names) is None


def test_llm_worker_runs_text_tool_call_then_writes_files(tmp_path):
    from shams.workers import LLMWorker
    calls = []

    class P:
        name, supports_tools = "fake", True
        def __init__(self):
            self.n = 0
        def chat_tools(self, msgs, *, model, tools, **o):
            self.n += 1
            if self.n == 1:
                return {"text": '{"name": "research__web_search", "arguments": {"query": "q"}}', "tool_calls": [],
                        "assistant": {"role": "assistant", "content": ""}}
            return {"text": json.dumps({"files": [{"logical_id": "code.a", "content": "x = 1"}], "summary": "ok",
                                        "status": "READY_FOR_QC"}), "tool_calls": [], "assistant": {}}
        def tool_results(self, results):
            return [{"role": "tool", "content": t} for _, t in results]

    class M:
        def tools(self, servers):
            return [{"name": "research__web_search", "server": "research", "input_schema": {"type": "object"}}]
        def call(self, name, args, catalog):
            calls.append((name, args))
            return {"text": "results", "is_error": False}
        research = type("R", (), {"enabled": True})()

    ctx = {"attempt": 1, "inputs": [], "project_id": "p", "goal": "g",
           "task": {"task_id": "task.a", "title": "A", "capability": "code.x", "acceptance_criteria": [],
                    "outputs": [{"name": "a", "logical_id": "code.a", "media_type": "text/x-python"}]}}
    w = LLMWorker(entry={"worker": {"model": "m"}}, attempts_root=tmp_path, provider=P(), mcp=M())
    files, h = w.run(ctx, {"worker_id": "worker.x", "model": "m"})
    assert calls == [("research__web_search", {"query": "q"})]
    assert h["status"] == "READY_FOR_QC" and [f["logical_id"] for f in files] == ["code.a"]


def test_blender_script_model_is_swapped_not_stacked(tmp_path):
    from shams_tools.blender import Scene3DWorker
    log = []

    class P:
        def unload(self, m):
            log.append(("unload", m))
    w = Scene3DWorker(entry={"worker": {"model": "small3d"}, "provider_config": {"blender_model": "coder14b"}},
                      attempts_root=tmp_path, provider=P())
    w.build = lambda *a: (log.append(("build", w.blender_model)) or ({"ok": True}, "", {}))
    w._build_swapped({}, tmp_path, {}, "blender")
    assert log == [("unload", "small3d"), ("build", "coder14b"), ("unload", "coder14b")]


def test_glb_size_uses_node_transforms(tmp_path):
    """Blender exports objects with node scale/translation; the size check must see the world size."""
    g = make_glb(tmp_path / "s.glb", scale=1.0, nodes=[{"mesh": 0, "scale": [0.5, 1.7, 1], "translation": [3, 0, 0]}])
    size = assets.parse_glb(g)["bbox"]["size"]
    assert abs(size[0] - 0.5) < 1e-6 and abs(size[1] - 1.7) < 1e-6
    g2 = make_glb(tmp_path / "t.glb", scale=1.0,
                  nodes=[{"children": [1], "translation": [0, 1, 0]}, {"mesh": 0}, {"mesh": 0, "translation": [2, 0, 0]}])
    b = assets.parse_glb(g2)["bbox"]
    assert abs(b["size"][0] - 3.0) < 1e-6 and abs(b["min"][1] - 0.0) < 1e-6 and abs(b["max"][1] - 2.0) < 1e-6


def test_single_asset_parts_must_touch():
    from shams_tools.scene_json import detached_parts
    body = ({"name": "Body"}, [-0.2, 0.8, -0.1], [0.2, 1.4, 0.1])
    head = ({"name": "Head"}, [-0.1, 1.45, -0.1], [0.1, 1.65, 0.1])          # 5 cm above the body
    leg = ({"name": "Leg"}, [-0.1, 0.0, -0.05], [0.0, 0.8, 0.05])             # touches
    probs = detached_parts([body, head, leg])
    assert len(probs) == 1 and probs[0].startswith("Head") and "5 cm" in probs[0]
    assert detached_parts([body, leg]) == []


def test_detached_head_found_with_real_gaps_where_boxes_touch():
    """Regression from ROPY-PC: the head's box touched the arms' boxes, so a 5 cm head gap passed."""
    from shams_tools.blender import blender_physics
    objs = {"Head": ([-0.15, -0.15, 0.65], [0.15, 0.15, 0.95]), "Torso": ([-0.1, -0.1, 0.0], [0.1, 0.1, 0.6]),
            "Arm_Right": ([0.15, -0.05, 0.3], [0.25, 0.05, 0.7]), "Arm_Left": ([-0.25, -0.05, 0.3], [-0.15, 0.05, 0.7]),
            "Leg_Right": ([0.03, -0.07, -0.25], [0.17, 0.07, 0.45]), "Leg_Left": ([-0.17, -0.07, -0.25], [-0.03, 0.07, 0.45])}
    rep = {"objects": [{"name": n, "type": "MESH", "bbox": [lo, hi]} for n, (lo, hi) in objs.items()]}
    assert not any("not attached" in p for p in blender_physics(rep, single_asset=True))   # boxes alone miss it
    names = sorted(objs)
    gaps = {f"{a}|{b}": 0.0 for i, a in enumerate(names) for b in names[i + 1:]}
    for k in list(gaps):
        if "Head" in k:
            gaps[k] = 0.05 if "Torso" in k else 0.08
    rep["gaps"] = gaps
    probs = [p for p in blender_physics(rep, single_asset=True) if "not attached" in p]
    assert len(probs) == 1 and probs[0].startswith("Head") and "5 cm" in probs[0]


def test_research_reports_a_blocked_search_page_as_unavailable(monkeypatch):
    from shams import research
    r = research.Research({"enabled": True})
    def fake_get(url, search=False):
        r.last_status = 202
        return "<html>please verify you are human</html>", "text/html"
    monkeypatch.setattr(r, "_get", fake_get)
    out = r.call("research__web_search", {"query": "three.js setup"})
    assert out["is_error"] and "unavailable" in out["text"] and "own knowledge" in out["text"]


def test_research_uses_a_local_searxng_for_search_only():
    from shams import research
    srv = Server(lambda m, path, body: (200, {"results": [{"title": "three.js docs", "url": "https://threejs.org/docs",
                                                           "content": "manual"}]}))
    try:
        r = research.Research({"enabled": True, "search_url": srv.url + "/search?q={query}&format=json&engines=google"})
        out = r.call("research__web_search", {"query": "three.js"})
        assert not out["is_error"] and "threejs.org/docs" in out["text"]
        bad = r.call("research__web_fetch", {"url": srv.url + "/anything"})      # fetch still refuses local hosts
        assert bad["is_error"] and "local" in bad["text"]
    finally:
        srv.close()


def test_llm_worker_giving_up_without_outputs_is_a_failed_attempt(tmp_path):
    from shams.workers import LLMWorker

    class P:
        name = "fake"
        def chat(self, messages, *, model, **o):
            assert "never because a search was empty" in messages[0]["content"]
            return {"text": json.dumps({"files": [], "summary": "no search results", "status": "BLOCKED",
                                        "blocked_reason": "No relevant web search results found"})}
    ctx = {"attempt": 1, "inputs": [], "project_id": "p", "goal": "g",
           "task": {"task_id": "task.a", "title": "A", "capability": "code.x", "acceptance_criteria": [],
                    "outputs": [{"name": "a", "logical_id": "code.a", "media_type": "text/x-python"}]}}
    files, h = LLMWorker(entry={"worker": {"model": "m"}}, attempts_root=tmp_path, provider=P()).run(
        ctx, {"worker_id": "worker.x", "model": "m"})
    assert files == [] and h["status"] == "FAILED" and "own knowledge" in h["known_issues"][0]["issue"]


def _png(path: Path, w: int, h: int, pixel) -> Path:
    rows = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(w)) for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return path


def test_png_edge_check_catches_a_cropped_subject(tmp_path):
    grey = (200, 200, 200)
    def person(top, bottom):
        return lambda x, y: ((40 + (x * 7) % 90, 60, 120) if 24 <= x < 40 and top <= y < bottom else grey)
    whole = _png(tmp_path / "whole.png", 64, 64, person(6, 58))          # head to feet inside the frame
    cut = _png(tmp_path / "cut.png", 64, 64, person(6, 64))              # legs run off the bottom edge
    assert assets.check_png(whole, {"max_edge_subject": 0.02})[0] == "PASS"
    v, d = assets.check_png(cut, {"max_edge_subject": 0.02})
    assert v == "FAIL" and "bottom" in d and "cropped" in d and "not plain" in d
    assert assets.png_info(cut, edges=True)["edge_subject"]["top"] == 0
    def studio(x, y):                                                   # grey backdrop, lighter floor
        g = 150 + y * 80 // 63
        return (40 + (x * 7) % 90, 60, 120) if 24 <= x < 40 and 6 <= y < 58 else (g, g, g)
    grad = _png(tmp_path / "grad.png", 64, 64, studio)
    assert assets.check_png(grad, {"max_edge_subject": 0.04})[0] == "PASS"


def test_module_exports_tell_the_next_worker_how_to_import():
    from shams.workers import module_exports
    ex = module_exports('import x from "y";\nexport default class PlayerController {\n}\nexport const SPEED = 2;\n'
                        'export { a, b };\nfunction hidden() {}\n')
    assert ex == ["export default class PlayerController", "export const SPEED", "export { a, b }"]


def test_png_edge_check_can_ignore_the_sides(tmp_path):
    grey = (200, 200, 200)
    img = _png(tmp_path / "side.png", 64, 64,
               lambda x, y: (30, 30, 30) if (x < 3 and 20 < y < 40) or (24 <= x < 40 and 6 <= y < 58) else grey)
    assert assets.check_png(img, {"max_edge_subject": 0.04})[0] == "FAIL"                  # a shadow on the left
    assert assets.check_png(img, {"max_edge_subject": 0.04, "edge_sides": ["top", "bottom"], "min_colours": 1})[0] == "PASS"


def test_scene3d_json_prompt_includes_the_text_inputs(tmp_path):
    from shams_tools.blender import Scene3DWorker
    brief = tmp_path / "b.txt"
    brief.write_text("A woman in her late 20s, long black hair, red dress, white shoes.", encoding="utf-8")
    w = Scene3DWorker(entry={"worker": {"worker_id": "w", "model": "m"}, "provider_config": {}},
                      attempts_root=tmp_path, read_blob=lambda sha: brief)
    ctx = {"goal": "g", "inputs": [{"logical_id": "brief.npc", "media_type": "text/plain", "sha256": "x"},
                                   {"logical_id": "img", "media_type": "image/png", "sha256": "y"}],
           "task": {"title": "npc", "description": "humanoid", "capability": "mesh.procedural",
                    "acceptance_criteria": []}}
    user = json.loads(w._json_messages(ctx, 20000)[1]["content"])
    assert user["inputs"] == [{"name": "brief.npc", "text": brief.read_text(encoding="utf-8")}]


def test_game_code_goes_to_the_coder_unless_a_game_coder_is_set(tmp_path):
    from shams.models import ModelRegistry
    from shams.nodes.registry import NodeRegistry
    disc = {"status": "ok", "loaded": [], "models": [
        {"name": "qwen2.5-coder:14b", "capabilities": ["completion"], "size_mb": 9000},
        {"name": "hf.co/ccore/Qwen3.5-4B-3D-Coder:Q8_0", "capabilities": ["completion"], "size_mb": 4500}]}
    reg = ModelRegistry({"models": {"roles": {"coder": {"model": "qwen2.5-coder:14b"},
                                              "scene3d": {"model": "Qwen3.5-4B-3D-Coder:Q8_0"}}}}, ollama=disc)
    assert reg.binding("game_coder") is None                      # not set: the coder writes game code
    ws = {w["id"]: w for w in reg.workers()}
    assert "worker.role.game_coder" not in ws and "code.*" in ws["worker.role.coder"]["capabilities"]
    reg = ModelRegistry({"models": {"roles": {"coder": {"model": "qwen2.5-coder:14b"},
                                              "game_coder": {"model": "Qwen3.5-4B-3D-Coder:Q8_0"}}}}, ollama=disc)
    b = reg.binding("game_coder")
    assert b.ok and b.model_id == "hf.co/ccore/Qwen3.5-4B-3D-Coder:Q8_0"


def test_scheduler_prefers_the_most_specific_worker_and_repair_switches_to_the_next():
    from shams.nodes.registry import NodeRegistry, normalize_worker
    reg = NodeRegistry.from_dict({
        "node": [{"id": "node.local", "kind": "local", "cpu_slots": 2, "gpus": [{"index": 0, "vram_mb": 12000}]}],
        "worker": [{"id": "worker.role.coder", "node": "node.local", "kind": "llm_agent", "provider": "ollama",
                    "model": "coder", "capabilities": ["code.*", "integration.*"]},
                   {"id": "worker.role.game_coder", "node": "node.local", "kind": "llm_agent", "provider": "ollama",
                    "model": "game", "capabilities": ["code.game.*", "integration.game"]}]})
    task = {"capability": "code.game.web_typescript"}
    assert [w.worker_id for w in reg.preferred(task)] == ["worker.role.game_coder"]
    assert [w.worker_id for w in reg.ranked("code.game.web_typescript")] == ["worker.role.game_coder",
                                                                             "worker.role.coder"]
    assert [w.worker_id for w in reg.preferred({**task, "provider_hint": "worker.role.coder"})] == ["worker.role.coder"]
    assert [w.worker_id for w in reg.preferred({"capability": "code.web_typescript"})] == ["worker.role.coder"]
    assert [w.worker_id for w in reg.preferred({"capability": "integration.game"})] == ["worker.role.game_coder"]
