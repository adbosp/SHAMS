"""Simple 3D scenes without Blender (ADR-121): the 3D coding model writes a *scene description* in JSON,
SHAMS builds the GLB and renders evidence in pure Python.

Blender stays for complex scenes (modifiers, booleans, sculpting, rigs, terrain...). For a handful of
primitives with transforms, colours, a camera and lights, a JSON description is far easier for a 4B model
to get right than Blender Python, and the result is checked the same way (GLB parse, render, scene report).

Coordinates are glTF's: metres, +Y up, the camera looks at ``target``. Rotations are Euler degrees (X, Y, Z).
"""
from __future__ import annotations

import json
import math
import re
import struct
import zlib
from pathlib import Path
from typing import Any

SHAPES = ("box", "plane", "sphere", "cylinder", "cone", "torus")
MAX_OBJECTS = 300
COLOURS = {"red": (0.8, 0.1, 0.1), "green": (0.15, 0.6, 0.2), "blue": (0.15, 0.3, 0.8), "yellow": (0.9, 0.8, 0.1),
           "orange": (0.9, 0.45, 0.1), "purple": (0.5, 0.2, 0.7), "white": (0.92, 0.92, 0.92), "black": (0.05, 0.05, 0.05),
           "grey": (0.5, 0.5, 0.5), "gray": (0.5, 0.5, 0.5), "brown": (0.45, 0.28, 0.12), "pink": (0.95, 0.5, 0.7),
           "cyan": (0.1, 0.7, 0.8), "wood": (0.55, 0.36, 0.2), "stone": (0.55, 0.55, 0.52), "grass": (0.25, 0.55, 0.2),
           "water": (0.15, 0.4, 0.7), "gold": (0.85, 0.65, 0.15), "silver": (0.75, 0.75, 0.78)}

SCENE_JSON_PROMPT = """You design simple 3D scenes as JSON. Reply with ONE JSON object only (no prose, no code).
Units are metres, +Y is up, the ground is y=0. Rotations are degrees [x, y, z].
Schema:
{"objects": [{"name": "table_top", "shape": "box|plane|sphere|cylinder|cone|torus",
              "size": [w, h, d] (box) | [w, d] (plane), "radius": r, "height": h (cylinder/cone), "tube": t (torus),
              "position": [x, y, z], "rotation": [0, 0, 0], "color": "#8b5a2b" or [r, g, b] 0..1,
              "metallic": 0..1, "roughness": 0..1}],
 "camera": {"position": [x, y, z], "target": [x, y, z], "fov": 50},
 "lights": [{"type": "sun", "direction": [-0.4, -1, -0.3], "intensity": 3}, {"type": "point", "position": [x, y, z]}],
 "background": "#202830"}
Rules: a "plane" is already horizontal (lying on the ground, facing +Y): do NOT rotate it. Build each thing from
several primitives: a table = one thin top box + 4 thin leg boxes at the corners (not solid side panels); a chair =
seat + 4 legs + back. Position is the CENTRE of the shape, so an object resting on a surface at height h has
y = h + half its height (a sphere of radius r on a table top at h: y = h + r). Use distinct colours, meaningful
names, keep the camera looking at the scene. Stay within the polygon budget.
Example (a table with legs and a cup on it):
{"objects": [{"name": "floor", "shape": "plane", "size": [8, 8], "color": "#777777"},
 {"name": "table_top", "shape": "box", "size": [1.2, 0.06, 0.8], "position": [0, 0.72, 0], "color": "#8b5a2b"},
 {"name": "leg_fl", "shape": "box", "size": [0.06, 0.69, 0.06], "position": [0.55, 0.345, 0.35], "color": "#6b4423"},
 {"name": "leg_fr", "shape": "box", "size": [0.06, 0.69, 0.06], "position": [-0.55, 0.345, 0.35], "color": "#6b4423"},
 {"name": "leg_bl", "shape": "box", "size": [0.06, 0.69, 0.06], "position": [0.55, 0.345, -0.35], "color": "#6b4423"},
 {"name": "leg_br", "shape": "box", "size": [0.06, 0.69, 0.06], "position": [-0.55, 0.345, -0.35], "color": "#6b4423"},
 {"name": "cup", "shape": "cylinder", "radius": 0.04, "height": 0.1, "position": [0.2, 0.80, 0], "color": "#2e6fd8"}],
 "camera": {"position": [2.0, 1.4, 2.4], "target": [0, 0.6, 0], "fov": 45},
 "lights": [{"type": "sun", "direction": [-0.4, -1, -0.3], "intensity": 3}]}"""


class SceneError(ValueError):
    pass


# --------------------------------------------------------------------------- parse + normalise
def extract_json(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    if not raw:
        raise SceneError("reply contains no JSON object")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SceneError(f"JSON does not parse: {e}") from None


def _vec(v: Any, n: int, default: list[float], what: str) -> list[float]:
    if v is None:
        return list(default)
    if isinstance(v, (int, float)):
        return [float(v)] * n
    if isinstance(v, dict):
        v = [v.get(k, d) for k, d in zip("xyz", default)]
    if not isinstance(v, (list, tuple)) or len(v) < n or not all(isinstance(x, (int, float)) for x in v[:n]):
        raise SceneError(f"{what}: expected {n} numbers, got {v!r}")
    out = [float(x) for x in v[:n]]
    if not all(math.isfinite(x) for x in out):
        raise SceneError(f"{what}: numbers must be finite")
    return out


def _colour(v: Any, what: str) -> list[float]:
    if v is None:
        return [0.7, 0.7, 0.7]
    if isinstance(v, str):
        s = v.strip().lower()
        if s in COLOURS:
            return list(COLOURS[s])
        m = re.fullmatch(r"#?([0-9a-f]{6})", s)
        if not m:
            raise SceneError(f"{what}: colour {v!r} is not #rrggbb, [r, g, b] or a basic name")
        return [int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = _vec(v, 3, [0.7] * 3, what)
    if max(c) > 1.0:
        c = [x / 255 for x in c]
    return [min(max(x, 0.0), 1.0) for x in c]


def normalise(scene: dict) -> dict:
    """Validate and fill defaults. Raises SceneError with a message the model can act on."""
    if not isinstance(scene, dict):
        raise SceneError("top level must be a JSON object")
    objs = scene.get("objects")
    if not isinstance(objs, list) or not objs:
        raise SceneError('"objects" must be a non-empty list')
    if len(objs) > MAX_OBJECTS:
        raise SceneError(f"{len(objs)} objects > {MAX_OBJECTS}; merge or simplify")
    out, names = [], set()
    for i, o in enumerate(objs):
        if not isinstance(o, dict):
            raise SceneError(f"objects[{i}] must be an object")
        shape = str(o.get("shape") or o.get("type") or "").lower()
        shape = {"cube": "box", "cuboid": "box", "ball": "sphere", "ground": "plane"}.get(shape, shape)
        if shape not in SHAPES:
            raise SceneError(f"objects[{i}].shape {o.get('shape')!r}: use one of {', '.join(SHAPES)}")
        name = str(o.get("name") or f"{shape}_{i}")[:60]
        base, k = name, 2
        while name in names:
            name, k = f"{base}_{k}", k + 1
        names.add(name)
        w = f"objects[{i}] ({name})"
        e: dict[str, Any] = {"name": name, "shape": shape,
                             "position": _vec(o.get("position"), 3, [0, 0, 0], w + ".position"),
                             "rotation": _vec(o.get("rotation"), 3, [0, 0, 0], w + ".rotation"),
                             "scale": _vec(o.get("scale"), 3, [1, 1, 1], w + ".scale"),
                             "color": _colour(o.get("color") or o.get("colour"), w + ".color"),
                             "metallic": float(o.get("metallic", 0.0)), "roughness": float(o.get("roughness", 0.6))}
        if shape == "plane" and abs(abs(e["rotation"][0]) - 90) < 1e-6 and not any(e["rotation"][1:]):
            e["rotation"] = [0.0, 0.0, 0.0]        # three.js habit: PlaneGeometry is vertical there, flat here
            e["note"] = "rotation x=±90 ignored: planes are horizontal"
        if shape == "box":
            e["size"] = _vec(o.get("size"), 3, [1, 1, 1], w + ".size")
        elif shape == "plane":
            s = o.get("size")
            s = [s[0], s[-1]] if isinstance(s, list) and len(s) == 3 else s
            e["size"] = _vec(s, 2, [10, 10], w + ".size")
        else:
            sz = o.get("size")
            e["radius"] = float(o.get("radius", (sz[0] / 2 if isinstance(sz, list) and sz else 0.5)))
            e["height"] = float(o.get("height", (sz[1] if isinstance(sz, list) and len(sz) > 1 else 1.0)))
            e["tube"] = float(o.get("tube", e["radius"] * 0.3))
        dims = e.get("size") or [e["radius"], e["height"] if shape in ("cylinder", "cone") else e["radius"]]
        if min(dims) <= 0 or min(e["scale"]) == 0:
            raise SceneError(f"{w}: sizes must be > 0")
        if max(dims) * max(map(abs, e["scale"])) > 10000:
            raise SceneError(f"{w}: size over 10 km, check units (metres)")
        out.append(e)
    cam = scene.get("camera") if isinstance(scene.get("camera"), dict) else {}
    lights = []
    for j, l in enumerate(scene.get("lights") or []):
        if not isinstance(l, dict):
            continue
        t = str(l.get("type", "sun")).lower()
        t = "point" if t in ("point", "spot", "lamp", "bulb") else "sun"
        lights.append({"type": t, "intensity": float(l.get("intensity", 3.0 if t == "sun" else 30.0)),
                       "color": _colour(l.get("color"), f"lights[{j}].color") if l.get("color") else [1, 1, 1],
                       # a sun that "points up" means the direction *towards* the light: flip it
                       "direction": [(-x if (l.get("direction") or [0, -1, 0])[1:2] and (l.get("direction") or [0, -1, 0])[1] > 0 else x)
                                     for x in _vec(l.get("direction"), 3, [-0.4, -1, -0.3], f"lights[{j}].direction")],
                       "position": _vec(l.get("position"), 3, [3, 5, 3], f"lights[{j}].position")})
    return {"objects": out, "lights": lights,
            "camera": {"position": _vec(cam.get("position"), 3, [0, 0, 0], "camera.position") if cam.get("position") else None,
                       "target": _vec(cam.get("target"), 3, [0, 0, 0], "camera.target") if cam.get("target") else None,
                       "fov": min(max(float(cam.get("fov", 50)), 10.0), 120.0)},
            "background": _colour(scene.get("background") or "#1e2630", "background")}


# --------------------------------------------------------------------------- geometry
def _rot(deg: list[float]):
    ax, ay, az = (math.radians(d) for d in deg)
    cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    # R = Rz * Ry * Rx (X applied first)
    return [[cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
            [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
            [-sy, cy * sx, cy * cx]]


def _mul(m, v):
    return [m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2], m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2]]


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def primitive(o: dict, seg: int = 24) -> tuple[list, list, list, list]:
    """(positions, normals, uvs, triangle indices) in the object's local frame, before rotation/scale."""
    P, N, U, I = [], [], [], []

    def quad(a, b, c, d, n):
        k = len(P)
        P.extend([a, b, c, d]), N.extend([n] * 4), U.extend([[0, 0], [1, 0], [1, 1], [0, 1]])
        I.extend([k, k + 1, k + 2, k, k + 2, k + 3])

    s = o["shape"]
    if s == "box":
        x, y, z = (v / 2 for v in o["size"])
        quad([-x, -y, z], [x, -y, z], [x, y, z], [-x, y, z], [0, 0, 1])
        quad([x, -y, -z], [-x, -y, -z], [-x, y, -z], [x, y, -z], [0, 0, -1])
        quad([x, -y, z], [x, -y, -z], [x, y, -z], [x, y, z], [1, 0, 0])
        quad([-x, -y, -z], [-x, -y, z], [-x, y, z], [-x, y, -z], [-1, 0, 0])
        quad([-x, y, z], [x, y, z], [x, y, -z], [-x, y, -z], [0, 1, 0])
        quad([-x, -y, -z], [x, -y, -z], [x, -y, z], [-x, -y, z], [0, -1, 0])
    elif s == "plane":
        x, z = (v / 2 for v in o["size"])
        quad([-x, 0, z], [x, 0, z], [x, 0, -z], [-x, 0, -z], [0, 1, 0])
    elif s == "sphere":
        r, rings = o["radius"], max(seg // 2, 6)
        for i in range(rings + 1):
            th = math.pi * i / rings
            for j in range(seg + 1):
                ph = 2 * math.pi * j / seg
                n = [math.sin(th) * math.cos(ph), math.cos(th), math.sin(th) * math.sin(ph)]
                P.append([r * c for c in n]), N.append(n), U.append([j / seg, i / rings])
        for i in range(rings):
            for j in range(seg):
                a, b = i * (seg + 1) + j, (i + 1) * (seg + 1) + j
                I.extend([a, a + 1, b, b, a + 1, b + 1])
    elif s in ("cylinder", "cone"):
        r, h = o["radius"], o["height"]
        top = 0.0 if s == "cone" else r
        slope = r / h if s == "cone" else 0.0
        for j in range(seg + 1):
            ph = 2 * math.pi * j / seg
            c, si = math.cos(ph), math.sin(ph)
            n = _norm([c, slope, si])
            P.extend([[r * c, -h / 2, r * si], [top * c, h / 2, top * si]]), N.extend([n, n])
            U.extend([[j / seg, 0], [j / seg, 1]])
        for j in range(seg):
            a = 2 * j
            I.extend([a, a + 1, a + 2, a + 2, a + 1, a + 3])
        caps = [(-h / 2, r, -1)] + ([] if s == "cone" else [(h / 2, r, 1)])
        for y, rr, sgn in caps:
            k = len(P)
            P.append([0, y, 0]), N.append([0, sgn, 0]), U.append([0.5, 0.5])
            for j in range(seg + 1):
                ph = 2 * math.pi * j / seg
                P.append([rr * math.cos(ph), y, rr * math.sin(ph)]), N.append([0, sgn, 0])
                U.append([0.5 + 0.5 * math.cos(ph), 0.5 + 0.5 * math.sin(ph)])
            for j in range(seg):
                I.extend([k, k + 1 + j, k + 2 + j] if sgn < 0 else [k, k + 2 + j, k + 1 + j])
    elif s == "torus":
        R, t, rs = o["radius"], o["tube"], max(seg // 2, 8)
        for i in range(seg + 1):
            u = 2 * math.pi * i / seg
            for j in range(rs + 1):
                v = 2 * math.pi * j / rs
                n = [math.cos(v) * math.cos(u), math.sin(v), math.cos(v) * math.sin(u)]
                P.append([(R + t * math.cos(v)) * math.cos(u), t * math.sin(v), (R + t * math.cos(v)) * math.sin(u)])
                N.append(n), U.append([i / seg, j / rs])
        for i in range(seg):
            for j in range(rs):
                a, b = i * (rs + 1) + j, (i + 1) * (rs + 1) + j
                I.extend([a, b, a + 1, a + 1, b, b + 1])
    return P, N, U, I


def build(scene: dict) -> dict:
    """Mesh every object: local vertices with rotation and scale applied, translation kept on the node."""
    meshes = []
    for o in scene["objects"]:
        P, N, U, I = primitive(o)
        R, sc = _rot(o["rotation"]), o["scale"]
        P = [_mul(R, [p[0] * sc[0], p[1] * sc[1], p[2] * sc[2]]) for p in P]
        N = [_norm(_mul(R, [n[0] / sc[0], n[1] / sc[1], n[2] / sc[2]])) for n in N]
        meshes.append({"object": o, "P": P, "N": N, "U": U, "I": I})
    lo = [min(p[k] + m["object"]["position"][k] for m in meshes for p in m["P"]) for k in range(3)]
    hi = [max(p[k] + m["object"]["position"][k] for m in meshes for p in m["P"]) for k in range(3)]
    cam = dict(scene["camera"])
    focus = [m for m in meshes if m["object"]["shape"] != "plane"] or meshes      # frame the things, not the ground
    flo = [min(p[k] + m["object"]["position"][k] for m in focus for p in m["P"]) for k in range(3)]
    fhi = [max(p[k] + m["object"]["position"][k] for m in focus for p in m["P"]) for k in range(3)]
    centre = [(a + b) / 2 for a, b in zip(flo, fhi)]
    radius = max(math.dist(flo, fhi) / 2, 0.1)
    cam["target"] = cam["target"] or centre
    if not cam["position"] or math.dist(cam["position"], cam["target"]) < 1e-6:
        d = _norm([1.0, 0.8, 1.2])
        dist = radius / math.sin(math.radians(cam["fov"]) / 2) * 1.1
        cam["position"] = [c + x * dist for c, x in zip(cam["target"], d)]
        cam["auto"] = True
    return {"meshes": meshes, "camera": cam, "bbox": {"min": lo, "max": hi},
            "lights": scene["lights"] or [{"type": "sun", "direction": [-0.4, -1, -0.3], "intensity": 3.0, "color": [1, 1, 1]}],
            "background": scene["background"]}


def support_problems(built: dict, tol: float = 0.02) -> list[str]:
    """Physical sanity, with the fix spelled out for the model:
    * a smaller object whose centre sits over a larger box/cylinder and whose bottom is inside it (sinking);
    * anything that goes below the ground plane."""
    boxes = []
    for m in built["meshes"]:
        t = m["object"]["position"]
        lo = [min(p[k] for p in m["P"]) + t[k] for k in range(3)]
        hi = [max(p[k] for p in m["P"]) + t[k] for k in range(3)]
        boxes.append((m["object"], lo, hi))
    area = lambda lo, hi: (hi[0] - lo[0]) * (hi[2] - lo[2])  # noqa: E731
    out = []
    flats = [(o, lo, hi) for o, lo, hi in boxes if hi[1] - lo[1] < 1e-6]
    ground = min((lo[1] for o, lo, hi in flats), default=None)
    for a, alo, ahi in boxes:
        h = ahi[1] - alo[1]
        cx, cy, cz = [(alo[k] + ahi[k]) / 2 for k in range(3)]
        if ground is not None and h > 1e-6 and alo[1] < ground - tol:
            out.append(f"{a['name']} goes {round(ground - alo[1], 2)} m below the floor (its lowest point is "
                       f"y={round(alo[1], 2)}); move it up so its bottom is at y={round(ground, 2)}"
                       + (" or fix its rotation" if any(a["rotation"]) else ""))
            continue
        if h < 1e-6:
            continue
        # floating: bottom above the ground, nothing under its footprint reaches it
        if ground is not None and alo[1] > ground + tol:
            under = [bhi[1] for b, blo, bhi in boxes if b is not a and bhi[1] <= alo[1] + tol
                     and blo[0] < ahi[0] and bhi[0] > alo[0] and blo[2] < ahi[2] and bhi[2] > alo[2]]
            below = max(under, default=ground)
            if tol < alo[1] - below < 0.15:          # a small gap is a mistake; a big one is meant (lamp, bird)
                out.append(f"{a['name']} floats {round((alo[1] - below) * 100)} cm above what is under it "
                           f"(top at y={round(below, 3)}); lower it to y = {round(cy - (alo[1] - below), 3)} "
                           f"or make its supports taller")
        for b, blo, bhi in boxes:
            if b is a or b["shape"] not in ("box", "cylinder") or area(alo, ahi) >= area(blo, bhi):
                continue
            if not (blo[0] <= cx <= bhi[0] and blo[2] <= cz <= bhi[2]):
                continue
            top, bmid = bhi[1], (blo[1] + bhi[1]) / 2
            if cy > bmid and alo[1] < top - tol and cy < top + h:
                out.append(f"{a['name']} sinks {round((top - alo[1]) * 100)} cm into {b['name']} (top at "
                           f"y={round(top, 3)}); set {a['name']}.position y = {round(top + (cy - alo[1]), 3)}")
                break
    return out


# --------------------------------------------------------------------------- GLB
def _look_quat(pos, target):
    f = _norm([t - p for t, p in zip(target, pos)])
    up = [0, 1, 0] if abs(f[1]) < 0.999 else [0, 0, 1]
    r = _norm(_cross(f, up))
    u = _cross(r, f)
    m = [[r[0], u[0], -f[0]], [r[1], u[1], -f[1]], [r[2], u[2], -f[2]]]      # columns: right, up, back
    tr = m[0][0] + m[1][1] + m[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        return [(m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s, 0.25 * s]
    i = max(range(3), key=lambda k: m[k][k])
    j, k = (i + 1) % 3, (i + 2) % 3
    s = math.sqrt(1.0 + m[i][i] - m[j][j] - m[k][k]) * 2
    q = [0.0] * 4
    q[i], q[j], q[k], q[3] = 0.25 * s, (m[j][i] + m[i][j]) / s, (m[k][i] + m[i][k]) / s, (m[k][j] - m[j][k]) / s
    return q


def write_glb(built: dict, path: Path | str) -> dict:
    binary = bytearray()
    views, accessors, materials, mat_ix, gl_meshes, nodes = [], [], [], {}, [], []

    def add(data: bytes, target: int, comp: int, typ: str, count: int, mn=None, mx=None) -> int:
        while len(binary) % 4:
            binary.append(0)
        views.append({"buffer": 0, "byteOffset": len(binary), "byteLength": len(data), "target": target})
        binary.extend(data)
        a = {"bufferView": len(views) - 1, "componentType": comp, "count": count, "type": typ}
        if mn is not None:
            a.update(min=mn, max=mx)
        accessors.append(a)
        return len(accessors) - 1

    for m in built["meshes"]:
        o = m["object"]
        key = (tuple(round(c, 3) for c in o["color"]), round(o["metallic"], 2), round(o["roughness"], 2))
        if key not in mat_ix:
            mat_ix[key] = len(materials)
            materials.append({"name": f"{o['name']}_mat", "pbrMetallicRoughness": {
                "baseColorFactor": [*o["color"], 1.0], "metallicFactor": o["metallic"], "roughnessFactor": o["roughness"]}})
        P = m["P"]
        pa = add(struct.pack(f"<{3 * len(P)}f", *[c for p in P for c in p]), 34962, 5126, "VEC3", len(P),
                 [min(p[k] for p in P) for k in range(3)], [max(p[k] for p in P) for k in range(3)])
        na = add(struct.pack(f"<{3 * len(P)}f", *[c for n in m["N"] for c in n]), 34962, 5126, "VEC3", len(P))
        ua = add(struct.pack(f"<{2 * len(P)}f", *[c for u in m["U"] for c in u]), 34962, 5126, "VEC2", len(P))
        ia = add(struct.pack(f"<{len(m['I'])}I", *m["I"]), 34963, 5125, "SCALAR", len(m["I"]))
        gl_meshes.append({"name": o["name"], "primitives": [{"attributes": {"POSITION": pa, "NORMAL": na, "TEXCOORD_0": ua},
                                                            "indices": ia, "material": mat_ix[key]}]})
        nodes.append({"name": o["name"], "mesh": len(gl_meshes) - 1, "translation": o["position"]})
    cam = built["camera"]
    nodes.append({"name": "Camera", "camera": 0, "translation": cam["position"],
                  "rotation": _look_quat(cam["position"], cam["target"])})
    lights = []
    for i, l in enumerate(built["lights"]):
        lights.append({"name": f"light_{i}", "type": "directional" if l["type"] == "sun" else "point",
                       "color": l["color"], "intensity": l["intensity"]})
        n = {"name": f"light_{i}", "extensions": {"KHR_lights_punctual": {"light": i}}}
        if l["type"] == "sun":
            n["rotation"] = _look_quat([0, 0, 0], l["direction"])
        else:
            n["translation"] = l["position"]
        nodes.append(n)
    gltf = {"asset": {"version": "2.0", "generator": "SHAMS scene_json"}, "scene": 0,
            "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes, "meshes": gl_meshes,
            "materials": materials, "accessors": accessors, "bufferViews": views,
            "buffers": [{"byteLength": len(binary)}],
            "cameras": [{"type": "perspective", "perspective": {"yfov": math.radians(cam["fov"]), "aspectRatio": 1.0,
                                                                 "znear": 0.05, "zfar": 10000}}],
            "extensionsUsed": ["KHR_lights_punctual"], "extensions": {"KHR_lights_punctual": {"lights": lights}}}
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    while len(binary) % 4:
        binary.append(0)
    body = struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(binary), 0x004E4942) + bytes(binary)
    Path(path).write_bytes(struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body)
    return {"materials": [m["name"] for m in materials], "triangles": sum(len(m["I"]) // 3 for m in built["meshes"])}


# --------------------------------------------------------------------------- software render
def render(built: dict, path: Path | str, size: int = 384) -> dict:
    """Flat-shaded z-buffer render from the scene camera. Returns coverage (share of pixels with geometry)."""
    cam = built["camera"]
    pos, f = cam["position"], _norm([t - p for t, p in zip(cam["target"], cam["position"])])
    up = [0, 1, 0] if abs(f[1]) < 0.999 else [0, 0, 1]
    r = _norm(_cross(f, up))
    u = _cross(r, f)
    k = 1.0 / math.tan(math.radians(cam["fov"]) / 2)
    bg = [int(c * 255) for c in built["background"]]
    W = H = size
    zbuf = [math.inf] * (W * H)
    img = bytearray(bytes(bg) * (W * H))
    suns = [(_norm([-c for c in l["direction"]]), l) for l in built["lights"] if l["type"] == "sun"]
    points = [l for l in built["lights"] if l["type"] == "point"]
    for m in built["meshes"]:
        o = m["object"]
        t = o["position"]
        W_ = [[p[0] + t[0], p[1] + t[1], p[2] + t[2]] for p in m["P"]]
        V = []
        for p in W_:
            d = [p[0] - pos[0], p[1] - pos[1], p[2] - pos[2]]
            z = _dot(d, f)
            V.append((W / 2 * (1 + k * _dot(d, r) / z), H / 2 * (1 - k * _dot(d, u) / z), z) if z > 0.05 else None)
        I = m["I"]
        for ti in range(0, len(I), 3):
            a, b, c = I[ti], I[ti + 1], I[ti + 2]
            va, vb, vc = V[a], V[b], V[c]
            if va is None or vb is None or vc is None:
                continue
            area = (vb[0] - va[0]) * (vc[1] - va[1]) - (vb[1] - va[1]) * (vc[0] - va[0])
            if abs(area) < 1e-9:
                continue
            pa, pb, pc = W_[a], W_[b], W_[c]
            n = _norm(_cross([pb[i] - pa[i] for i in range(3)], [pc[i] - pa[i] for i in range(3)]))
            cen = [(pa[i] + pb[i] + pc[i]) / 3 for i in range(3)]
            if _dot(n, [pos[i] - cen[i] for i in range(3)]) < 0:
                n = [-x for x in n]
            light = 0.35
            for ld, l in suns:
                light += max(_dot(n, ld), 0.0) * min(l["intensity"], 10) / 3 * 0.8
            for l in points:
                v = [l["position"][i] - cen[i] for i in range(3)]
                dist2 = max(_dot(v, v), 0.25)
                light += max(_dot(n, _norm(v)), 0.0) * min(l["intensity"] / dist2, 3.0) * 0.3
            col = bytes(min(255, int(255 * ch * light)) for ch in o["color"])
            x0, x1 = max(int(min(va[0], vb[0], vc[0])), 0), min(int(max(va[0], vb[0], vc[0])) + 1, W - 1)
            y0, y1 = max(int(min(va[1], vb[1], vc[1])), 0), min(int(max(va[1], vb[1], vc[1])) + 1, H - 1)
            inv = 1.0 / area
            for y in range(y0, y1 + 1):
                py = y + 0.5
                for x in range(x0, x1 + 1):
                    px = x + 0.5
                    w0 = ((vb[0] - px) * (vc[1] - py) - (vb[1] - py) * (vc[0] - px)) * inv
                    w1 = ((vc[0] - px) * (va[1] - py) - (vc[1] - py) * (va[0] - px)) * inv
                    w2 = 1.0 - w0 - w1
                    if w0 < 0 or w1 < 0 or w2 < 0:
                        continue
                    z = w0 * va[2] + w1 * vb[2] + w2 * vc[2]
                    ix = y * W + x
                    if z < zbuf[ix]:
                        zbuf[ix] = z
                        img[ix * 3: ix * 3 + 3] = col
    rows = b"".join(b"\x00" + bytes(img[y * W * 3:(y + 1) * W * 3]) for y in range(H))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))
    covered = sum(1 for z in zbuf if z < math.inf)
    return {"coverage": round(covered / (W * H), 4), "resolution": size}


# --------------------------------------------------------------------------- one call
def build_scene(text_or_scene: str | dict, glb: Path | str | None, png: Path | str | None, *, resolution: int = 384,
                max_triangles: int | None = None, min_coverage: float = 0.01, check_support: bool = True) -> dict:
    """Parse -> normalise -> mesh -> GLB -> render. Returns a scene report like the Blender harness's.
    Raises SceneError with a fixable message."""
    scene = normalise(extract_json(text_or_scene) if isinstance(text_or_scene, str) else text_or_scene)
    built = build(scene)
    tris = sum(len(m["I"]) // 3 for m in built["meshes"])
    sinking = support_problems(built)
    if sinking and check_support:
        raise SceneError("physical problems: " + "; ".join(sinking[:6]))
    if max_triangles and tris > max_triangles:
        raise SceneError(f"{tris} triangles > budget {max_triangles}; use fewer spheres/cylinders/tori")
    rep: dict[str, Any] = {"ok": True, "builder": "scene_json", "mesh_objects": len(built["meshes"]),
                           "cameras": 1, "camera_added": bool(built["camera"].get("auto")), "lights": len(built["lights"]),
                           "triangles": tris, "bbox": built["bbox"],
                           "notes": [f"{o['name']}: {o['note']}" for o in scene["objects"] if o.get("note")],
                           "warnings": sinking,      # left after the last fix round (check_support=False)
                           "objects": [{"name": m["object"]["name"], "type": "MESH", "shape": m["object"]["shape"],
                                        "location": m["object"]["position"], "vertices": len(m["P"]),
                                        "faces": len(m["I"]) // 3, "triangles": len(m["I"]) // 3, "non_manifold_edges": 0,
                                        "has_uv": True, "materials": [f"{m['object']['name']}_mat"]} for m in built["meshes"]]}
    if glb:
        rep.update(write_glb(built, glb))
    else:
        rep["materials"] = sorted({f"{m['object']['name']}_mat" for m in built["meshes"]})
    if png:
        rep.update(render(built, png, resolution))
        if rep["coverage"] < min_coverage:
            raise SceneError(f"the camera sees almost nothing (coverage {rep['coverage']:.1%}); point camera.target "
                             f"at the objects (scene bbox {built['bbox']}) or omit the camera to auto-frame")
    return rep
