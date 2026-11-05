"""Plugin discovery (ADR-116).

Everything model-, engine- or tool-specific is a plugin that can be added without editing SHAMS:

* a dotted reference ``"package.module:Attr"`` in a config file, or
* a Python entry point in one of the groups below (``pip install shams-plugin-x`` is enough).

Groups: ``shams.providers``, ``shams.workers``, ``shams.engines``, ``shams.checkers``, ``shams.templates``.
"""
from __future__ import annotations

import importlib
import os
from importlib import metadata
from pathlib import Path
from typing import Any

GROUPS = ("shams.providers", "shams.workers", "shams.engines", "shams.checkers", "shams.templates")


def load_object(ref: str) -> Any:
    """Import ``"package.module:Attr"`` (``Attr`` may be dotted)."""
    if ":" not in ref:
        raise ValueError(f"plugin reference must look like 'package.module:Name', got {ref!r}")
    mod_name, attr = ref.split(":", 1)
    obj: Any = importlib.import_module(mod_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def entry_points(group: str) -> dict[str, metadata.EntryPoint]:
    try:
        eps = metadata.entry_points(group=group)
    except TypeError:  # pragma: no cover - very old importlib.metadata
        eps = metadata.entry_points().get(group, [])
    return {ep.name: ep for ep in eps}


def shams_home() -> Path:
    """User-level folder for shared config and plugins (``SHAMS_HOME``, default ``~/.shams``)."""
    return Path(os.environ.get("SHAMS_HOME") or Path.home() / ".shams")


def search_dirs(kind: str, project_dir: Path | str | None = None) -> list[Path]:
    """Where file-based plugins of ``kind`` (engines, templates) are looked up, most specific first."""
    out = []
    if project_dir is not None:
        out.append(Path(project_dir) / kind)
    out.append(shams_home() / kind)
    return [p for p in out if p.is_dir()]
