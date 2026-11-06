"""Runs inside Blender: ``blender -b --factory-startup -P blender_harness.py -- spec.json``.

spec = {"script": path | null, "import_glb": path | null, "glb": path | null, "render": path | null,
        "report": path, "resolution": 512, "engine": "BLENDER_WORKBENCH"}

Starts from an empty scene, runs the generated scene script (or imports a GLB for an import check),
collects geometry/material statistics, frames a camera if the script made none, renders evidence and
exports GLB. Always writes ``report``; exit code 0 = ok, 3 = the script or the export failed.
"""
import json
import math
import sys
import traceback

import bpy  # noqa: E402  (only available inside Blender)
import bmesh  # noqa: E402
from mathutils import Vector  # noqa: E402


def _stats(scene):
    objs = []
    for o in scene.objects:
        e = {"name": o.name, "type": o.type, "location": [round(v, 4) for v in o.location],
             "dimensions": [round(v, 4) for v in o.dimensions]}
        if o.type == "MESH":
            me = o.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()
            bm = bmesh.new()
            bm.from_mesh(me)
            e.update(vertices=len(bm.verts), faces=len(bm.faces),
                     triangles=sum(len(f.verts) - 2 for f in bm.faces),
                     non_manifold_edges=sum(1 for ed in bm.edges if not ed.is_manifold),
                     loose_verts=sum(1 for v in bm.verts if not v.link_edges),
                     zero_area_faces=sum(1 for f in bm.faces if f.calc_area() < 1e-10),
                     has_uv=bool(me.uv_layers),
                     materials=[m.name for m in o.data.materials if m])
            ws = [o.matrix_world @ Vector(c) for c in o.bound_box]
            e["bbox"] = [[round(min(v[k] for v in ws), 4) for k in range(3)],
                         [round(max(v[k] for v in ws), 4) for k in range(3)]]
            bm.free()
            o.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh_clear()
        objs.append(e)
    return objs


def _fit_height(scene, height):
    """Scale the imported asset uniformly to ``height`` metres (Z up), feet on the ground, centred on the origin."""
    from mathutils import Matrix
    meshes = [o for o in scene.objects if o.type == "MESH"]
    if not meshes:
        return None
    bpy.context.view_layer.update()
    lo, hi = _bbox(meshes)
    h = hi.z - lo.z
    if h <= 1e-6:
        return None
    k = height / h
    centre = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
    m = Matrix.Scale(k, 4) @ Matrix.Translation(-centre)
    for o in scene.objects:
        if o.parent is None:
            o.matrix_world = m @ o.matrix_world
    bpy.context.view_layer.update()
    return round(k, 4)


def _gaps(meshes, limit=40, samples=1500):
    """Real surface distance between every pair of meshes (world space, metres; 0 = touching or overlapping).
    Bounding boxes are not enough: the box of a sphere touches things the sphere never reaches."""
    from mathutils.bvhtree import BVHTree
    if len(meshes) < 2 or len(meshes) > limit:
        return None
    dg = bpy.context.evaluated_depsgraph_get()
    data = {}
    for o in meshes:
        me = o.evaluated_get(dg).to_mesh()
        verts = [o.matrix_world @ v.co for v in me.vertices]
        polys = [tuple(p.vertices) for p in me.polygons]
        o.evaluated_get(dg).to_mesh_clear()
        if not verts or not polys:
            continue
        step = max(1, len(verts) // samples)
        data[o.name] = (BVHTree.FromPolygons(verts, polys), verts[::step])
    out = {}
    names = sorted(data)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ta, va = data[a]
            tb, vb = data[b]
            if ta.overlap(tb):
                d = 0.0
            else:
                d = min([r[3] for r in (tb.find_nearest(v) for v in va) if r[0] is not None] +
                        [r[3] for r in (ta.find_nearest(v) for v in vb) if r[0] is not None] or [math.inf])
            out[f"{a}|{b}"] = round(d, 4)
    return out


def _bbox(meshes):
    lo, hi = Vector((math.inf,) * 3), Vector((-math.inf,) * 3)
    for o in meshes:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            lo = Vector(map(min, lo, w))
            hi = Vector(map(max, hi, w))
    return lo, hi


def _frame_camera(scene, meshes, direction=None):
    lo, hi = _bbox(meshes)
    center = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.1)
    cam_data = bpy.data.cameras.new("SHAMS_Camera")
    cam = bpy.data.objects.new("SHAMS_Camera", cam_data)
    scene.collection.objects.link(cam)
    direction = Vector(direction or (1.0, -1.2, 0.8)).normalized()
    cam.location = center + direction * radius * 3.2
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    cam_data.clip_end = max(1000.0, radius * 20)
    scene.camera = cam
    return True


def _ensure_uvs(meshes):
    """Meshes built vertex by vertex often have no UV map; game engines need one. Smart UV Project fills it."""
    added = []
    for o in meshes:
        if o.data.uv_layers:
            continue
        bpy.ops.object.select_all(action="DESELECT")
        o.select_set(True)
        bpy.context.view_layer.objects.active = o
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project()
        bpy.ops.object.mode_set(mode="OBJECT")
        added.append(o.name)
    return added


def main():
    spec = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
    report = {"ok": False, "blender": bpy.app.version_string}
    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        scene = bpy.context.scene
        if spec.get("import_glb"):
            bpy.ops.import_scene.gltf(filepath=spec["import_glb"])
            report["stage"] = "import"
            if spec.get("fit_height"):
                report["scaled_by"] = _fit_height(scene, float(spec["fit_height"]))
        if spec.get("script"):
            code = open(spec["script"], encoding="utf-8").read()
            report["stage"] = "script"
            exec(compile(code, "scene_script.py", "exec"), {"__name__": "__main__"})
            scene = bpy.context.scene
        meshes = [o for o in scene.objects if o.type == "MESH"]
        report["objects"] = _stats(scene)
        report["mesh_objects"] = len(meshes)
        report["cameras"] = sum(1 for o in scene.objects if o.type == "CAMERA")
        report["lights"] = sum(1 for o in scene.objects if o.type == "LIGHT")
        report["materials"] = sorted({m for e in report["objects"] for m in e.get("materials", [])})
        if meshes:
            lo, hi = _bbox(meshes)
            report["bbox"] = {"min": list(lo), "max": list(hi), "size": list(hi - lo)}
            try:
                report["gaps"] = _gaps(meshes)
            except Exception as e:  # noqa: BLE001 - a measurement must not fail the build
                report["gaps_error"] = f"{type(e).__name__}: {e}"
        if spec.get("render"):
            report["stage"] = "render"
            if not meshes:
                raise RuntimeError("render evidence impossible: the scene has no mesh objects")
            # evidence must show the subject: SHAMS frames its own camera on the meshes (a script camera that
            # misses the object would make the render, and the visual QC on it, worthless)
            report["camera_added"] = _frame_camera(scene, meshes, spec.get("camera_dir"))
            scene.render.engine = spec.get("engine") or "BLENDER_WORKBENCH"
            if scene.render.engine == "BLENDER_WORKBENCH":
                scene.display.shading.light = "STUDIO"
                textured = any(n.type == "TEX_IMAGE" and n.image is not None for mat in bpy.data.materials
                               if mat.use_nodes and mat.node_tree for n in mat.node_tree.nodes)
                scene.display.shading.color_type = "TEXTURE" if textured else "MATERIAL"   # show SF3D colours
                report["preview_colour"] = scene.display.shading.color_type
                # Workbench shows material.diffuse_color, not the node colour: copy Base Color across
                for mat in bpy.data.materials:
                    bsdf = mat.node_tree.nodes.get("Principled BSDF") if mat.use_nodes and mat.node_tree else None
                    if bsdf is not None and "Base Color" in bsdf.inputs:
                        mat.diffuse_color = tuple(bsdf.inputs["Base Color"].default_value)
            res = int(spec.get("resolution", 512))
            scene.render.resolution_x = scene.render.resolution_y = res
            scene.render.resolution_percentage = 100
            scene.render.image_settings.file_format = "PNG"
            scene.render.filepath = spec["render"]
            bpy.ops.render.render(write_still=True)
        if spec.get("glb"):
            report["stage"] = "uv"
            report["uv_added"] = _ensure_uvs(meshes)
            report["stage"] = "export"
            bpy.ops.export_scene.gltf(filepath=spec["glb"], export_format="GLB", export_apply=True)
        report["ok"] = True
        report.pop("stage", None)
    except Exception:  # noqa: BLE001 - the report carries the traceback back to the worker
        report["error"] = traceback.format_exc()[-4000:]
    with open(spec["report"], "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    sys.exit(0 if report["ok"] else 3)


main()
