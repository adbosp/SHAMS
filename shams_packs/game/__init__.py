"""Game Development Pack (ADR-115, ADR-117).

Everything game-specific lives here: engine profiles (Unity, Unreal Engine 5, Godot, Web/Three.js,
custom engines from engines/*.toml), engine-neutral game templates, game failure patterns and
mock logs. SHAMS Core only sees a DomainPack.
"""
from __future__ import annotations

from pathlib import Path

from shams.packs import DomainPack

from . import checks, engines

GAME_WORDS = ("game", "trò chơi", "gameplay", "playable", "chơi được", "màn chơi", "level design", "fps", "rpg",
              "platformer", "vertical slice", "npc")


class GamePack(DomainPack):
    name = "game"
    display = "Game"
    description = "Game development: engine profiles, gameplay templates, engine automation"
    default_template = "vertical_slice_3d"

    # ---------------------------------------------------------------- configuration
    def options(self, project_dir=None):
        engines.load_plugins(project_dir)
        return {"engine": [{"id": p.name, "display": p.display, "language": p.language}
                           for p in engines.ENGINES.values()]}

    def default_config(self) -> dict:
        p = engines.get("unity")
        return {"engine": {"name": p.name, "version": p.default_version}}

    def normalize_config(self, config, project_dir=None) -> dict:
        engines.load_plugins(project_dir)
        cfg = {**self.default_config(), **(config or {})}
        e = cfg["engine"]
        name, version = (e, None) if isinstance(e, str) else (e.get("name", "unity"), e.get("version"))
        prof = engines.get(name)
        cfg["engine"] = {"name": prof.name,
                         "version": version if version and isinstance(e, dict) and e.get("name") == prof.name
                         else prof.default_version}
        return cfg

    def describe(self, config) -> str:
        return engines.get(config["engine"]["name"]).display

    def load_plugins(self, project_dir=None) -> list[str]:
        return engines.load_plugins(project_dir)

    # ---------------------------------------------------------------- requests
    def match_score(self, text: str) -> int:
        low = text.lower()
        return 2 * sum(w in low for w in GAME_WORDS) + (3 if engines.detect(text) else 0)

    def detect(self, text: str) -> dict:
        e = engines.detect(text)
        return {"engine": e} if e else {}

    # ---------------------------------------------------------------- planning
    def templates_dir(self) -> Path:
        return Path(__file__).resolve().parent / "templates"

    def template_tokens(self, config) -> dict[str, str]:
        return engines.get(config["engine"]["name"]).tokens()

    def template_label(self, config) -> str:
        return engines.get(config["engine"]["name"]).name

    # ---------------------------------------------------------------- execution & QC
    def domain_context(self, config) -> dict:
        p = engines.get(config["engine"]["name"])
        return {"engine": p.name, "engine_display": p.display, "version": config["engine"]["version"],
                "language": p.language, "roots": dict(p.roots), "build_target": p.build_target}

    def failure_rules(self):
        return [
            (r"error CS\d+", "compile_error", "fix_in_place"),                       # Unity / C#
            (r"(?i)error C\d{4}|LNK\d{4}", "compile_error", "fix_in_place"),          # Unreal / MSVC
            (r"(?i)SCRIPT ERROR|Parse Error", "compile_error", "fix_in_place"),       # Godot
        ]

    def checkers(self) -> dict:
        return {"compile": checks.compile_check, "unit_test": checks.unit_test_check}

    def mock_failure_logs(self) -> dict[str, str]:
        return {"compile": "Assets/Scripts/X.cs(42,13): error CS0103: The name 'moveSpeed' does not exist "
                           "in the current context"}

    def worker_labels(self) -> dict[str, str]:
        return {"scene": "Scene Capture", "build": "Build & Test Tools"}

    def media_types(self):
        return {"application/vnd.unity.scene": (".unity", True), "application/vnd.unity.prefab": (".prefab", True),
                "application/vnd.godot.scene": (".tscn", True), "text/x-gdscript": (".gd", True),
                "application/vnd.unreal.map": (".umap", False)}

    def template_aliases(self):
        return {"unity_3d_vertical_slice": ("vertical_slice_3d", {"engine": "unity"})}


PACK = GamePack()
