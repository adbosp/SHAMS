"""3D route policy (ADR-120), cheapest first:
reuse an existing asset -> primitives -> procedural geometry -> modular kit -> placeholder
-> image-to-3D (Stable Fast 3D), only with a reference image AND a genuinely complex object.
"""
from __future__ import annotations

import re

ROUTES = ("reuse", "primitives", "procedural", "modular", "placeholder", "image_to_3d")
COMPLEX_HINTS = re.compile(r"(?i)\b(organic|character|creature|statue|sculpt|photo ?real|scanned|detailed (?:prop|model)|"
                           r"nhân vật|sinh vật|tượng|chi tiết cao)\b")


def choose_3d_route(task: dict, inputs: list[dict], *, sf3d_available: bool) -> tuple[str, str]:
    """Returns (route, reason). Never picks image_to_3d for basic scenes."""
    text = f"{task.get('title', '')} {task.get('description', '')}"
    hint = (task.get("provider_hint") or "").lower()
    if any(i.get("media_type") in ("model/gltf-binary", "model/gltf+json", "model/fbx", "model/obj") for i in inputs):
        return "reuse", "an upstream 3D asset exists; adapt it instead of generating"
    images = [i for i in inputs if str(i.get("media_type", "")).startswith("image/")]
    complex_ = bool(COMPLEX_HINTS.search(text)) or "complex" in hint
    if images and complex_ and sf3d_available:
        return "image_to_3d", "reference image + complex object"
    if images and complex_:
        return "procedural", "complex object with reference image, but Stable Fast 3D is not configured"
    if re.search(r"(?i)\b(placeholder|blockout|greybox|graybox|giữ chỗ)\b", text):
        return "placeholder", "task asks for a blockout"
    if re.search(r"(?i)\b(modular|kit|tile|module)\b", text):
        return "modular", "modular kit pieces"
    if re.search(r"(?i)\b(cube|box|sphere|cylinder|plane|primitive|khối)\b", text) and not complex_:
        return "primitives", "primitive shapes are enough"
    return "procedural", "procedural geometry in Blender"




BLENDER_HINTS = re.compile(
    r"(?i)\b(complex|phức tạp|modifier|boolean|bevel|subdivi\w*|sculpt\w*|rig\w*|armature|animat\w*|keyframe|"
    r"terrain|landscape|địa hình|physics|particle\w*|cloth|hair|fluid|curve|bezier|spline|text mesh|"
    r"geometry nodes|displace\w*|procedural (?:city|building|terrain|texture)|high[- ]poly|interior with|"
    r"architectur\w*|vehicle|car|spaceship|tàu vũ trụ|xe)\b")


def scene_builder(task: dict, *, blender_available: bool) -> tuple[str, str]:
    """Who builds a scene: "json" (the 3D coding model writes a scene description, SHAMS builds GLB + render)
    or "blender" (the model writes Blender Python). Blender only for complex scenes (user rule, ADR-121)."""
    want = str((task.get("params") or {}).get("builder") or "").lower()
    if want in ("json", "blender"):  # "auto" or empty: decide from the text
        return want, f"builder={want} requested"
    text = f"{task.get('title', '')} {task.get('description', '')}"
    m = BLENDER_HINTS.search(text) or COMPLEX_HINTS.search(text)
    if m:
        return "blender", f"complex scene ({m.group(0)!r})"
    return "json", "simple scene: primitives described as JSON, no Blender needed"
