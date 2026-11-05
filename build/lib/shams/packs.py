"""Domain Packs (ADR-117, SHAMS V2 §3).

SHAMS Core knows nothing about a particular product domain. A Domain Pack contributes the
domain knowledge: configuration options (e.g. a game engine or a software stack), plan
templates and their tokens, the context workers receive, extra failure-classification rules,
QC checkers, worker labels and scope heuristics.

Packs are found, in order:
* packs registered in code with ``register_pack``;
* the ``shams.packs`` entry point group (``pip install`` a pack);
* the built-in packs listed in ``BUILTIN`` (loaded lazily by reference, never imported by core).

Core modules must not import ``shams_packs``; a test enforces this.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from shams import plugins

BUILTIN: dict[str, str] = {
    "software": "shams_packs.software:PACK",
    "game": "shams_packs.game:PACK",
}
DEFAULT_DOMAIN = "software"


class DomainPack:
    """Base class. Override what the domain needs; defaults are neutral."""

    name: str = "generic"
    display: str = "Generic"
    description: str = ""
    default_template: str | None = None

    # ---------------------------------------------------------------- configuration
    def options(self, project_dir: Path | str | None = None) -> dict[str, list[dict]]:
        """Choosable options, e.g. {"stack": [{"id": "python", "display": "Python", "language": "Python"}]}."""
        return {}

    def default_config(self) -> dict:
        return {}

    def normalize_config(self, config: dict, project_dir: Path | str | None = None) -> dict:
        """Validate and fill a domain_config. Raise KeyError/ValueError on invalid values."""
        return {**self.default_config(), **(config or {})}

    def configure(self, config: dict, project_dir: Path | str | None = None, **options: Any) -> dict:
        """Apply user choices (e.g. stack="typescript") to an existing config."""
        allowed = set(self.options(project_dir))
        unknown = sorted(set(options) - allowed)
        if unknown:
            raise KeyError(f"domain {self.name!r} has no option {unknown}; options: {sorted(allowed)}")
        return self.normalize_config({**(config or {}), **options}, project_dir)

    def describe(self, config: dict) -> str:
        """Short human label, e.g. 'Python'."""
        return self.display

    # ---------------------------------------------------------------- understanding requests
    def match_score(self, text: str) -> int:
        """How strongly a request belongs to this domain (0 = not at all)."""
        return 0

    def detect(self, text: str) -> dict:
        """Option hints found in a request, e.g. {"engine": "web"}."""
        return {}

    # ---------------------------------------------------------------- planning
    def templates_dir(self) -> Path | None:
        return None

    def template_tokens(self, config: dict) -> dict[str, str]:
        return {}

    def template_label(self, config: dict) -> str:
        """Suffix recorded in dag.template, e.g. 'python'."""
        return self.name

    def load_plugins(self, project_dir: Path | str | None = None) -> list[str]:
        """Load file-based extensions of this pack (e.g. custom engines)."""
        return []

    # ---------------------------------------------------------------- execution & QC
    def domain_context(self, config: dict) -> dict:
        """What workers receive as context_package.domain_context."""
        return dict(config or {})

    def failure_rules(self) -> list[tuple[str, str, str]]:
        """Extra (regex, failure_class, strategy) rules, checked before the core rules."""
        return []

    def checkers(self) -> dict:
        return {}

    def mock_failure_logs(self) -> dict[str, str]:
        """Realistic failure logs for the Mock Worker, keyed by criterion kind."""
        return {}

    def worker_labels(self) -> dict[str, str]:
        """capability prefix -> label shown in the UI."""
        return {}

    def media_types(self) -> dict[str, tuple[str, bool]]:
        """media_type -> (file extension, is_text) for domain-specific formats."""
        return {}

    def template_aliases(self) -> dict[str, tuple[str, dict]]:
        """Old template names: alias -> (template, domain_config)."""
        return {}

    def activate(self, project_dir: Path | str | None = None) -> None:
        """Called when a project of this domain opens: register media types and file plugins."""
        from shams import media
        for mt, (ext, text) in self.media_types().items():
            media.register(mt, ext, text)
        self.load_plugins(project_dir)


_REGISTERED: dict[str, DomainPack] = {}
_LOADED: dict[str, DomainPack] = {}


def register_pack(pack: DomainPack) -> DomainPack:
    _REGISTERED[pack.name] = pack
    _LOADED.pop(pack.name, None)
    return pack


def _instantiate(obj: Any) -> DomainPack:
    pack = obj() if isinstance(obj, type) else obj
    if not isinstance(pack, DomainPack):
        raise TypeError(f"{obj!r} is not a DomainPack")
    return pack


def available() -> list[str]:
    return sorted(set(_REGISTERED) | set(plugins.entry_points("shams.packs")) | set(BUILTIN))


def get(name: str | None) -> DomainPack:
    name = (name or DEFAULT_DOMAIN).strip().lower()
    if name in _REGISTERED:
        return _REGISTERED[name]
    if name in _LOADED:
        return _LOADED[name]
    eps = plugins.entry_points("shams.packs")
    if name in eps:
        pack = _instantiate(eps[name].load())
    elif name in BUILTIN:
        pack = _instantiate(plugins.load_object(BUILTIN[name]))
    else:
        raise KeyError(f"unknown domain {name!r}; available: {available()}")
    _LOADED[name] = pack
    return pack


def all_packs() -> list[DomainPack]:
    out = []
    for n in available():
        try:
            out.append(get(n))
        except Exception:  # a broken third-party pack must not break the others
            continue
    return out


def detect_domain(text: str) -> str | None:
    """The domain whose pack scores highest for a request, or None."""
    best, score = None, 0
    for p in all_packs():
        s = p.match_score(text)
        if s > score:
            best, score = p.name, s
    return best
