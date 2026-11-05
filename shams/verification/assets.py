"""Binary asset inspection without third-party libraries (ADR-120): GLB (glTF 2.0 binary) and PNG.

Used by the QC checkers for 3D scenes, image-to-3D meshes, generated images and render evidence.
"""
from __future__ import annotations

import json
import math
import struct
import zlib
from pathlib import Path

_COMP = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}


class AssetError(ValueError):
    pass


# --------------------------------------------------------------------------- GLB
def parse_glb(path: str | Path) -> dict:
    """Validate the container and summarise geometry. Raises AssetError on a broken file."""
    data = Path(path).read_bytes()
    if len(data) < 20:
        raise AssetError("file too small for GLB")
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise AssetError("not a GLB file (magic != glTF)")
    if version != 2:
        raise AssetError(f"glTF version {version}, expected 2")
    if length != len(data):
        raise AssetError(f"header length {length} != file size {len(data)} (truncated?)")
    off, gltf, binchunk = 12, None, b""
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from("<II", data, off)
        chunk = data[off + 8: off + 8 + clen]
        if len(chunk) != clen:
            raise AssetError("truncated chunk")
        if ctype == 0x4E4F534A:
            try:
                gltf = json.loads(chunk.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise AssetError(f"JSON chunk invalid: {e}") from None
        elif ctype == 0x004E4942:
            binchunk = chunk
        off += 8 + clen + ((4 - clen % 4) % 4 if clen % 4 else 0)
    if gltf is None:
        raise AssetError("no JSON chunk")
    acc, views = gltf.get("accessors", []), gltf.get("bufferViews", [])
    for i, a in enumerate(acc):
        bv = a.get("bufferView")
        if bv is None:
            continue
        if bv >= len(views):
            raise AssetError(f"accessor {i}: bufferView {bv} out of range")
        v = views[bv]
        need = v.get("byteOffset", 0) + a.get("byteOffset", 0) + \
            (a["count"] - 1) * v.get("byteStride", _COMP.get(a["componentType"], 4) * _NCOMP.get(a["type"], 1)) + \
            _COMP.get(a["componentType"], 4) * _NCOMP.get(a["type"], 1)
        if v.get("buffer", 0) == 0 and binchunk and need > len(binchunk):
            raise AssetError(f"accessor {i} reads past the binary chunk")
    verts = tris = prims = 0
    has_uv = has_normals = True
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for m in gltf.get("meshes", []):
        for p in m.get("primitives", []):
            prims += 1
            at = p.get("attributes", {})
            if "POSITION" not in at:
                raise AssetError("primitive without POSITION")
            pa = acc[at["POSITION"]]
            verts += pa["count"]
            if "min" in pa and "max" in pa:
                lo = [min(a, b) for a, b in zip(lo, pa["min"])]
                hi = [max(a, b) for a, b in zip(hi, pa["max"])]
            has_uv &= "TEXCOORD_0" in at
            has_normals &= "NORMAL" in at
            mode = p.get("mode", 4)
            n = acc[p["indices"]]["count"] if "indices" in p else pa["count"]
            tris += n // 3 if mode == 4 else max(0, n - 2) if mode in (5, 6) else 0
    if prims == 0:
        has_uv = has_normals = False
    bbox = None if lo[0] == math.inf else {"min": lo, "max": hi, "size": [h - l for l, h in zip(lo, hi)]}
    mats = gltf.get("materials", [])
    textured = any("baseColorTexture" in (m.get("pbrMetallicRoughness") or {}) for m in mats)
    return {"meshes": len(gltf.get("meshes", [])), "primitives": prims, "nodes": len(gltf.get("nodes", [])),
            "vertices": verts, "triangles": tris, "materials": len(mats), "textures": len(gltf.get("textures", [])),
            "images": len(gltf.get("images", [])), "textured": textured, "has_uv": has_uv, "has_normals": has_normals,
            "bbox": bbox, "generator": (gltf.get("asset") or {}).get("generator", ""), "bytes": len(data)}


def check_glb(path: str | Path, check: dict | None = None) -> tuple[str, str]:
    """check: min_vertices, max_triangles, require_uv, require_texture, require_material,
    min_size_m / max_size_m (largest bbox side, glTF units are metres)."""
    c = check or {}
    try:
        g = parse_glb(path)
    except (AssetError, OSError, KeyError, IndexError, struct.error) as e:
        return "FAIL", f"GLB invalid: {e}"
    errs = []
    if g["vertices"] < int(c.get("min_vertices", 3)):
        errs.append(f"only {g['vertices']} vertices")
    if c.get("max_triangles") and g["triangles"] > int(c["max_triangles"]):
        errs.append(f"{g['triangles']} triangles > budget {c['max_triangles']}")
    if c.get("require_uv") and not g["has_uv"]:
        errs.append("no UV (TEXCOORD_0)")
    if c.get("require_texture") and not g["textured"]:
        errs.append("no base colour texture")
    if c.get("require_material") and g["materials"] == 0:
        errs.append("no material")
    if g["bbox"]:
        big = max(g["bbox"]["size"])
        if not all(math.isfinite(x) for x in g["bbox"]["size"]):
            errs.append("bounding box not finite")
        elif "min_size_m" in c and big < float(c["min_size_m"]):
            errs.append(f"largest side {big:.3f} m < {c['min_size_m']} m (scale?)")
        elif "max_size_m" in c and big > float(c["max_size_m"]):
            errs.append(f"largest side {big:.3f} m > {c['max_size_m']} m (scale?)")
    elif c.get("min_size_m") or c.get("max_size_m"):
        errs.append("POSITION accessor lacks min/max, cannot check scale")
    summary = (f"{g['meshes']} mesh, {g['vertices']} verts, {g['triangles']} tris, {g['materials']} materials, "
               f"uv={g['has_uv']}, textured={g['textured']}"
               + (f", size={'x'.join(f'{s:.2f}' for s in g['bbox']['size'])} m" if g["bbox"] else ""))
    return ("FAIL", "; ".join(errs) + f" ({summary})") if errs else ("PASS", summary)


# --------------------------------------------------------------------------- PNG
def png_info(path: str | Path) -> dict:
    """Size, colour type and pixel statistics of an 8-bit non-interlaced PNG."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssetError("not a PNG file")
    off, idat, ihdr = 8, b"", None
    while off + 8 <= len(data):
        n, typ = struct.unpack_from(">I4s", data, off)
        body = data[off + 8: off + 8 + n]
        if typ == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        off += 12 + n
    if not ihdr:
        raise AssetError("PNG without IHDR")
    w, h, depth, ctype, _, _, interlace = ihdr
    info = {"width": w, "height": h, "bit_depth": depth, "color_type": ctype}
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if depth != 8 or interlace or ch is None:
        return info                                     # stats need 8-bit, non-interlaced
    raw = zlib.decompress(idat)
    stride = w * ch
    prev = bytearray(stride)
    vals_sum = vals_sq = 0
    count = 0
    colours: set[bytes] = set()
    step = max(1, h // 128)
    adj_sum = 0.0                                       # neighbour differences: noise vs. a real picture
    adj_n = 0
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        if y % step == 0:
            colour_ch = 3 if ch >= 3 else 1
            lum = [sum(line[x * ch: x * ch + colour_ch]) / colour_ch for x in range(w)]
            adj_sum += sum(abs(lum[x + 1] - lum[x]) for x in range(w - 1))
            adj_n += max(w - 1, 0)
            for x in range(0, w, max(1, w // 128)):
                px = bytes(line[x * ch: x * ch + colour_ch])
                colours.add(px)
                v = sum(px) / len(px)                  # luminance-ish, so a flat colour has std 0
                vals_sum += v
                vals_sq += v * v
                count += 1
    mean = vals_sum / max(count, 1)
    std = math.sqrt(max(vals_sq / max(count, 1) - mean * mean, 0))
    # mean neighbour difference / std: ~1.1 for per-pixel noise, ~0.34 for a broken diffusion decode (VAE colour
    # noise), 0.01-0.08 for real photos and renders measured on ROPY-PC
    rough = (adj_sum / adj_n) / std if adj_n and std > 0 else 0.0
    info.update(mean=round(mean, 2), std=round(std, 2), distinct_colours=len(colours), roughness=round(rough, 3))
    return info


def check_png(path: str | Path, check: dict | None = None) -> tuple[str, str]:
    """check: min_width, min_height, min_std (not blank), min_colours, max_roughness (not random noise)."""
    c = check or {}
    try:
        i = png_info(path)
    except (AssetError, OSError, zlib.error, struct.error) as e:
        return "FAIL", f"image invalid: {e}"
    errs = []
    if i["width"] < int(c.get("min_width", 16)) or i["height"] < int(c.get("min_height", 16)):
        errs.append(f"too small {i['width']}x{i['height']}")
    if "std" in i:
        if i["std"] < float(c.get("min_std", 3.0)):
            errs.append(f"image is blank/flat (std {i['std']})")
        if i["distinct_colours"] < int(c.get("min_colours", 8)):
            errs.append(f"only {i['distinct_colours']} colours")
        if i["roughness"] > float(c.get("max_roughness", 0.25)):
            errs.append(f"image looks like random noise (neighbour/std ratio {i['roughness']})")
    summary = f"{i['width']}x{i['height']}" + (f", std {i['std']}, {i['distinct_colours']} colours, roughness "
                                               f"{i['roughness']}" if "std" in i else "")
    return ("FAIL", "; ".join(errs) + f" ({summary})") if errs else ("PASS", summary)
