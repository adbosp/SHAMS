"""Data contracts v1: JSON Schema validation (docs/design/03_Contracts.md)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).parent / "schemas"
SCHEMA_VERSION = "1.0"


class ContractError(ValueError):
    def __init__(self, schema: str, errors: list[str]):
        super().__init__(f"{schema}: " + "; ".join(errors))
        self.schema = schema
        self.errors = errors


@lru_cache(maxsize=1)
def _registry() -> tuple[Registry, dict[str, dict]]:
    schemas = {p.name.removesuffix(".schema.json"): json.loads(p.read_text(encoding="utf-8"))
               for p in SCHEMA_DIR.glob("*.schema.json")}
    reg = Registry().with_resources([(s["$id"], Resource.from_contents(s)) for s in schemas.values()])
    return reg, schemas


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    reg, schemas = _registry()
    if name not in schemas:
        raise KeyError(f"unknown schema {name!r}")
    return Draft202012Validator(schemas[name], registry=reg)


def errors(name: str, obj: Any) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in _validator(name).iter_errors(obj)]


def validate(name: str, obj: Any) -> Any:
    errs = errors(name, obj)
    if errs:
        raise ContractError(name, errs)
    return obj


def check_id(value: str, what: str = "id") -> str:
    """Raise ValueError unless `value` matches common.schema.json#/$defs/id (e.g. a project id)."""
    pattern = _registry()[1]["common"]["$defs"]["id"]["pattern"]
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"invalid {what} {value!r}: use 2-128 characters, start with a lowercase letter, "
                         f"then lowercase letters, digits, '_', '.' or '-' (pattern {pattern})")
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
