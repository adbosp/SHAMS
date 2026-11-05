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
def make_glb(path: Path, *, uv=True, scale=1.0, truncate=False) -> Path:
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
            "meshes": [{"primitives": [{"attributes": attrs, "material": 0}]}], "nodes": [{"mesh": 0}]}
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
    assert all(b.ok for b in by.values()), [b.as_dict() for b in by.values()]
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
    assert b.status == "down" and not models.ModelRegistry(s).workers()
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
