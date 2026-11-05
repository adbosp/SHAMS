"""Media types known to SHAMS: file extension and whether workers treat the content as text.

Core ships generic types; Domain Packs add their own with ``register`` (ADR-117).
"""
from __future__ import annotations

_EXT: dict[str, str] = {
    "text/plain": ".txt", "text/markdown": ".md", "text/html": ".html", "text/css": ".css",
    "text/x-python": ".py", "text/typescript": ".ts", "text/javascript": ".js", "text/x-c++src": ".cpp",
    "text/x-csharp": ".cs", "text/x-rust": ".rs", "text/x-go": ".go", "text/x-java": ".java",
    "application/json": ".json", "application/toml": ".toml", "application/x-yaml": ".yaml",
    "application/xml": ".xml", "application/javascript": ".js", "image/svg+xml": ".svg",
    "image/png": ".png", "image/jpeg": ".jpg", "model/gltf-binary": ".glb", "model/gltf+json": ".gltf",
    "model/fbx": ".fbx", "model/obj": ".obj", "audio/wav": ".wav", "audio/ogg": ".ogg",
    "application/zip": ".zip", "application/x-msdownload": ".exe",
}
_TEXT: set[str] = {"application/json", "application/toml", "application/x-yaml", "application/xml",
                   "application/javascript", "image/svg+xml", "model/gltf+json"}


def register(media_type: str, extension: str, text: bool = False) -> None:
    _EXT[media_type] = extension if extension.startswith(".") else "." + extension
    if text:
        _TEXT.add(media_type)


def extension(media_type: str) -> str:
    return _EXT.get(media_type, ".bin")


def is_text(media_type: str) -> bool:
    return media_type.startswith("text/") or media_type in _TEXT or media_type.endswith("+json")
