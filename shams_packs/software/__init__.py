"""Software Development Pack (ADR-117): applications, libraries, CLIs, APIs, websites.

Stacks are profiles like the Game Pack's engines. Real checkers (pytest, tsc, vitest) arrive with
V2.2; until then compile/test criteria end as NOT_VERIFIED unless a checker is registered.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shams.packs import DomainPack


@dataclass(frozen=True)
class Stack:
    name: str
    display: str
    language: str
    tokens: dict
    words: tuple[str, ...]


STACKS: dict[str, Stack] = {
    "python": Stack("python", "Python", "Python", {
        "code": "code.python", "test": "test.python", "build": "build.python",
        "src": "src/app", "tests": "tests", "dist": "dist", "ext": "py",
        "script_mt": "text/x-python", "script_contract": "python.module", "test_contract": "python.pytest",
        "manifest": "pyproject.toml", "manifest_name": "pyproject", "manifest_mt": "application/toml",
        "build_mt": "application/zip", "build_contract": "python.wheel",
        "test_tool": "pytest", "stack_display": "Python"},
        ("python", "django", "flask", "fastapi", "pytest", "pandas")),
    "typescript": Stack("typescript", "TypeScript (Node.js)", "TypeScript", {
        "code": "code.web_typescript", "test": "test.web", "build": "build.node",
        "src": "src", "tests": "tests", "dist": "dist", "ext": "ts",
        "script_mt": "text/typescript", "script_contract": "web.ts_module", "test_contract": "web.vitest",
        "manifest": "package.json", "manifest_name": "package", "manifest_mt": "application/json",
        "build_mt": "application/zip", "build_contract": "node.package",
        "test_tool": "vitest", "stack_display": "TypeScript"},
        ("typescript", "node.js", "nodejs", "next.js", "nextjs", "react", "vue", "svelte", "express")),
}
ALIASES = {"py": "python", "ts": "typescript", "node": "typescript", "javascript": "typescript", "js": "typescript"}
SOFTWARE_WORDS = ("app", "ứng dụng", "phần mềm", "website", "web app", "api", "backend", "frontend", "cli",
                  "script", "library", "thư viện", "tool", "công cụ", "service", "dashboard", "bot",
                  "refactor", "debug", "unit test", "codebase")


def get_stack(name: str) -> Stack:
    key = ALIASES.get(name.strip().lower(), name.strip().lower())
    if key not in STACKS:
        raise KeyError(f"unknown stack {name!r}; choose one of {sorted(STACKS)}")
    return STACKS[key]


class SoftwarePack(DomainPack):
    name = "software"
    display = "Software"
    description = "Software development: apps, libraries, CLIs, APIs, websites"
    default_template = "software_basic"

    def options(self, project_dir=None):
        return {"stack": [{"id": s.name, "display": s.display, "language": s.language} for s in STACKS.values()]}

    def default_config(self) -> dict:
        return {"stack": "python"}

    def normalize_config(self, config, project_dir=None) -> dict:
        cfg = {**self.default_config(), **(config or {})}
        cfg["stack"] = get_stack(cfg["stack"]).name
        return cfg

    def describe(self, config) -> str:
        return get_stack(config["stack"]).display

    def match_score(self, text: str) -> int:
        low = text.lower()
        return sum(w in low for w in SOFTWARE_WORDS) + (2 if self.detect(text) else 0)

    def detect(self, text: str) -> dict:
        low = text.lower()
        for s in STACKS.values():
            if any(w in low for w in s.words):
                return {"stack": s.name}
        return {}

    def templates_dir(self) -> Path:
        return Path(__file__).resolve().parent / "templates"

    def template_tokens(self, config) -> dict[str, str]:
        return dict(get_stack(config["stack"]).tokens)

    def template_label(self, config) -> str:
        return get_stack(config["stack"]).name

    def domain_context(self, config) -> dict:
        s = get_stack(config["stack"])
        return {"stack": s.name, "language": s.language, "test_tool": s.tokens["test_tool"],
                "roots": {"src": s.tokens["src"], "tests": s.tokens["tests"]}}

    def failure_rules(self):
        return [
            (r"(?i)SyntaxError|IndentationError|ModuleNotFoundError|ImportError", "compile_error", "fix_in_place"),
            (r"error TS\d+", "compile_error", "fix_in_place"),
            (r"(?i)\d+ failed|AssertionError|FAIL\s", "test_failure", "fix_in_place"),
        ]

    def mock_failure_logs(self) -> dict[str, str]:
        return {"compile": "src/app/core.py:42: SyntaxError: invalid syntax",
                "unit_test": "FAILED tests/test_core.py::{criterion} - AssertionError: expected 1.0, got 0.0"}


PACK = SoftwarePack()
