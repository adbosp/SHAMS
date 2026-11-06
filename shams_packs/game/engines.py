"""Engine Profiles and Engine Adapters for the Game Pack (ADR-115, ADR-117).

A game plan is written once (engine-neutral template) and instantiated for one engine profile.
The profile decides capabilities, file layout, asset formats, artifact contracts and the
commands real adapters run for compile / test / build / capture (Spike S2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class EngineProfile:
    name: str                    # unity | unreal | godot | web
    display: str
    default_version: str
    language: str
    code_capability: str
    build_capability: str
    test_capability: str
    script_media_type: str
    script_contract: str
    test_contract: str
    scene_media_type: str
    scene_contract: str
    mesh_media_type: str
    mesh_contract: str
    build_media_type: str
    build_contract: str
    build_target: str
    roots: dict[str, str] = field(default_factory=dict)  # scripts, tests, scenes, models, textures, audio, builds
    notes: str = ""
    # how the integration step exposes the playable scene, and the QC check types for running/building it
    # ("" = no checker yet: NOT_VERIFIED)
    scene_hint: str = ""
    test_hint: str = ""      # how unit tests must be written for this engine's test runner
    run_check: str = ""
    build_check: str = ""
    # Optional argv per step (compile/test/build/capture); "{project}" and "{exe}" are substituted.
    commands: dict[str, list[str]] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()
    detect_words: tuple[str, ...] = ()

    REQUIRED_ROOTS = ("scripts", "tests", "scenes", "models", "textures", "audio", "builds")

    @classmethod
    def from_dict(cls, d: dict) -> "EngineProfile":
        """Build a custom engine from TOML/JSON (ADR-116). See shams_packs/game/engine.example.toml."""
        d = dict(d)
        missing = [r for r in cls.REQUIRED_ROOTS if r not in d.get("roots", {})]
        if missing:
            raise ValueError(f"engine {d.get('name')!r}: roots missing {missing}")
        d["aliases"] = tuple(d.get("aliases", ()))
        d["detect_words"] = tuple(w.lower() for w in d.get("detect_words", ()))
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"engine {d.get('name')!r}: unknown fields {sorted(unknown)}")
        return cls(**d)

    def tokens(self) -> dict[str, str]:
        t = {
            "engine": self.name, "engine_display": self.display, "language": self.language,
            "code": self.code_capability, "build": self.build_capability, "test": self.test_capability,
            "script_mt": self.script_media_type, "script_contract": self.script_contract,
            "test_contract": self.test_contract, "scene_mt": self.scene_media_type,
            "scene_contract": self.scene_contract, "mesh_mt": self.mesh_media_type,
            "mesh_contract": self.mesh_contract, "build_mt": self.build_media_type,
            "build_contract": self.build_contract, "build_target": self.build_target,
            # QC check type for the mesh: GLB is inspected for real; other formats have no checker yet
            "mesh_check": "glb" if self.mesh_media_type == "model/gltf-binary" else "mesh_unverified",
            "scene_hint": self.scene_hint, "test_hint": self.test_hint, "run_check": self.run_check, "build_check": self.build_check,
        }
        t.update(self.roots)
        return t


ENGINES: dict[str, EngineProfile] = {
    "unity": EngineProfile(
        name="unity", display="Unity", default_version="6000.0", language="C#",
        code_capability="code.unity_csharp", build_capability="build.unity", test_capability="test.unity",
        script_media_type="text/x-csharp", script_contract="unity.csharp_script", test_contract="unity.test_script",
        scene_media_type="application/vnd.unity.scene", scene_contract="unity.scene",
        mesh_media_type="model/gltf-binary", mesh_contract="mesh.fbx_game_ready",
        build_media_type="application/x-msdownload", build_contract="build.windows", build_target="Windows",
        roots={"scripts": "Assets/Scripts", "tests": "Assets/Tests", "scenes": "Assets/Scenes",
               "models": "Assets/Models", "textures": "Assets/Textures", "audio": "Assets/Audio",
               "builds": "Builds"},
        notes="Unity Test Framework (EditMode/PlayMode) qua -batchmode -runTests."),
    "unreal": EngineProfile(
        name="unreal", display="Unreal Engine 5", default_version="5.4", language="C++",
        code_capability="code.unreal_cpp", build_capability="build.unreal", test_capability="test.unreal",
        script_media_type="text/x-c++src", script_contract="unreal.cpp_module", test_contract="unreal.automation_test",
        scene_media_type="application/vnd.unreal.map", scene_contract="unreal.map",
        mesh_media_type="model/fbx", mesh_contract="mesh.fbx_game_ready",
        build_media_type="application/x-msdownload", build_contract="build.windows", build_target="Win64",
        roots={"scripts": "Source/Game", "tests": "Source/Game/Tests", "scenes": "Content/Maps",
               "models": "Content/Models", "textures": "Content/Textures", "audio": "Content/Audio",
               "builds": "Builds"},
        notes="Automation Tests qua UnrealEditor-Cmd -nullrhi; build qua RunUAT BuildCookRun. "
              "Asset nhập qua FBX/glTF import (Interchange), không sinh .uasset trực tiếp."),
    "godot": EngineProfile(
        name="godot", display="Godot", default_version="4.3", language="GDScript",
        code_capability="code.godot_gdscript", build_capability="build.godot", test_capability="test.godot",
        script_media_type="text/x-gdscript", script_contract="godot.gdscript", test_contract="godot.gut_test",
        scene_media_type="application/vnd.godot.scene", scene_contract="godot.scene",
        mesh_media_type="model/gltf-binary", mesh_contract="mesh.gltf_game_ready",
        build_media_type="application/x-msdownload", build_contract="build.windows", build_target="Windows Desktop",
        roots={"scripts": "scripts", "tests": "test", "scenes": "scenes", "models": "assets/models",
               "textures": "assets/textures", "audio": "assets/audio", "builds": "builds"},
        notes="Test bằng GUT (--headless); export qua --export-release với export_presets.cfg."),
    "web": EngineProfile(
        name="web", display="Three.js (WebGPU)", default_version="three@0.170", language="TypeScript",
        code_capability="code.web_typescript", build_capability="build.web", test_capability="test.web",
        script_media_type="text/typescript", script_contract="web.ts_module", test_contract="web.vitest",
        scene_media_type="text/typescript", scene_contract="web.scene_module",
        mesh_media_type="model/gltf-binary", mesh_contract="mesh.gltf_game_ready",
        build_media_type="application/zip", build_contract="build.web", build_target="Static web (Vite)",
        roots={"scripts": "src/game", "tests": "tests", "scenes": "src/scenes", "models": "public/models",
               "textures": "public/textures", "audio": "public/audio", "builds": "dist"},
        scene_hint=("Module scene phải `export function start(container: HTMLElement): void`: tạo renderer "
                    "(WebGPU, lùi về WebGL2), camera, ánh sáng, mặt đất phủ texture /textures/environment.png, nạp "
                    "/models/Player/player.glb và /models/Npc/npc.glb bằng GLTFLoader (three/examples/jsm/loaders/"
                    "GLTFLoader.js), gắn điều khiển nhân vật, spawner, cơ chế chính và HUD từ các module đầu vào, rồi "
                    "chạy vòng lặp requestAnimationFrame. SHAMS tự tạo index.html và src/main.ts gọi start(); không "
                    "viết hai file đó."),
        test_hint=("Test viết cho Vitest: `import { describe, it, expect, vi } from \"vitest\"`, mock bằng `vi.fn()` / "
                   "`vi.spyOn()`, không dùng `jest`. File test đặt cùng thư mục với module nó test và import bằng "
                   "đường dẫn tương đối `./TenModule`."),
        run_check="web_smoke", build_check="web_zip",
        notes="WebGPURenderer, tự lùi về WebGL2 khi trình duyệt không có WebGPU. Build Vite, test Vitest, "
              "smoke test + screenshot bằng Playwright headless."),
}

ALIASES = {
    "unity": "unity", "unity6": "unity",
    "unreal": "unreal", "ue5": "unreal", "ue": "unreal", "unreal engine": "unreal", "unreal engine 5": "unreal",
    "godot": "godot", "godot4": "godot",
    "web": "web", "threejs": "web", "three.js": "web", "three": "web", "webgpu": "web", "webgl": "web",
}

# Keywords for deterministic engine detection from a chat request (the LLM Planner replaces this in 1b).
_DETECT = [("web", ("three.js", "threejs", "webgpu", "webgl", "trình duyệt", "browser", "web game")),
           ("unreal", ("ue5", "unreal")), ("godot", ("godot",)), ("unity", ("unity",))]


def register(profile: EngineProfile) -> EngineProfile:
    """Add or replace an engine profile at runtime (custom engines, ADR-116)."""
    ENGINES[profile.name] = profile
    for a in (profile.name, *profile.aliases):
        ALIASES[a.lower()] = profile.name
    if profile.detect_words:
        global _DETECT
        _DETECT = [(n, w) for n, w in _DETECT if n != profile.name]
        _DETECT.insert(0, (profile.name, profile.detect_words))
    return profile


def load_file(path: Path | str) -> EngineProfile:
    import json
    import tomllib
    p = Path(path)
    data = tomllib.loads(p.read_text(encoding="utf-8")) if p.suffix == ".toml" else json.loads(p.read_text("utf-8"))
    return register(EngineProfile.from_dict(data.get("engine", data)))


def load_plugins(project_dir: Path | str | None = None) -> list[str]:
    """Register engines from entry points (group shams.engines) and from engines/*.toml|json in the
    project folder and in $SHAMS_HOME. Returns the names loaded."""
    from shams import plugins
    names = []
    for ep in plugins.entry_points("shams.engines").values():
        obj = ep.load()
        prof = obj if isinstance(obj, EngineProfile) else EngineProfile.from_dict(obj() if callable(obj) else obj)
        names.append(register(prof).name)
    for d in reversed(plugins.search_dirs("engines", project_dir)):   # project wins over user-level
        for f in sorted(d.glob("*.toml")) + sorted(d.glob("*.json")):
            names.append(load_file(f).name)
    return names


def get(name: str) -> EngineProfile:
    key = ALIASES.get(name.strip().lower())
    if key is None:
        raise KeyError(f"unknown engine {name!r}; choose one of {sorted(ENGINES)} or add engines/<name>.toml")
    return ENGINES[key]


def detect(text: str) -> str | None:
    low = text.lower()
    for name, words in _DETECT:
        if any(w in low for w in words):
            return name
    return None


# --------------------------------------------------------------------------- adapters (commands)
class EngineAdapter:
    """Builds the argv a real checker runs. Execution and log parsing land with Spike S2.

    `exe` is the engine executable (Unity.exe, UnrealEditor-Cmd.exe / RunUAT.bat, godot, npm).
    """

    def __init__(self, profile: EngineProfile, project: Path, exe: str | None = None):
        self.p = profile
        self.project = Path(project)
        self.exe = exe

    def commands(self) -> dict[str, list[str]]:
        P, n = str(self.project), self.p.name
        if self.p.commands:
            exe = self.exe or ""
            return {k: [a.format(project=P, exe=exe) for a in argv] for k, argv in self.p.commands.items()}
        if n == "unity":
            exe = self.exe or "Unity"
            base = [exe, "-batchmode", "-nographics", "-projectPath", P, "-logFile", "-"]
            return {
                "compile": base + ["-quit"],
                "test": base + ["-runTests", "-testPlatform", "PlayMode", "-testResults", "results.xml"],
                "build": base + ["-executeMethod", "Shams.BuildScript.BuildWindows", "-quit"],
                "capture": [exe, "-projectPath", P, "-executeMethod", "Shams.Capture.Screenshot", "-quit"],
            }
        if n == "unreal":
            editor = self.exe or "UnrealEditor-Cmd"
            uproject = f"{P}/Game.uproject"
            return {
                "compile": ["RunUAT", "BuildEditor", f"-project={uproject}", "-notools"],
                "test": [editor, uproject, '-ExecCmds=Automation RunTests Shams;Quit', "-unattended", "-nullrhi",
                         "-nosplash", "-log"],
                "build": ["RunUAT", "BuildCookRun", f"-project={uproject}", "-platform=Win64",
                          "-clientconfig=Development", "-build", "-cook", "-stage", "-pak", "-archive",
                          f"-archivedirectory={P}/Builds"],
                "capture": [editor, uproject, "-game", "-ExecCmds=HighResShot 1920x1080;Quit", "-unattended"],
            }
        if n == "godot":
            exe = self.exe or "godot"
            return {
                "compile": [exe, "--headless", "--path", P, "--import", "--quit"],
                "test": [exe, "--headless", "--path", P, "-s", "addons/gut/gut_cmdln.gd", "-gexit"],
                "build": [exe, "--headless", "--path", P, "--export-release", self.p.build_target,
                          f"{P}/builds/game.exe"],
                "capture": [exe, "--path", P, "--", "--shams-screenshot=capture.png"],
            }
        if n != "web":
            raise KeyError(f"engine {n!r} declares no commands; add [engine.commands] to its profile")
        npm = self.exe or "npm"
        return {
            "compile": [npm, "run", "typecheck", "--prefix", P],
            "test": [npm, "test", "--prefix", P],
            "build": [npm, "run", "build", "--prefix", P],
            "capture": ["npx", "playwright", "screenshot", "http://127.0.0.1:4173", f"{P}/capture.png"],
        }
