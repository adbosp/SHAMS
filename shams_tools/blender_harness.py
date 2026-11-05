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
            bm.free()
            o.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh_clear()
        objs.append(e)
    return objs


def _bbox(meshes):
    lo, hi = Vector((math.inf,) * 3), Vector((-math.inf,) * 3)
    for o in meshes:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            lo = Vector(map(min, lo, w))
            hi = Vector(map(max, hi, w))
    return lo, hi


def _frame_camera(scene, meshes):
    lo, hi = _bbox(meshes)
    center = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.1)
    cam_data = bpy.data.cameras.new("SHAMS_Camera")
    cam = bpy.data.objects.new("SHAMS_Camera", cam_data)
    scene.collection.objects.link(cam)
    direction = Vector((1.0, -1.2, 0.8)).normalized()
    cam.location = center + direction * radius * 2.8
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    cam_data.clip_end = max(1000.0, radius * 20)
    scene.camera = cam
    return True


def main():
    spec = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
    report = {"ok": False, "blender": bpy.app.version_string}
    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        scene = bpy.context.scene
        if spec.get("import_glb"):
            bpy.ops.import_scene.gltf(filepath=spec["import_glb"])
            report["stage"] = "import"
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
        if spec.get("render"):
            report["stage"] = "render"
            if not meshes:
                raise RuntimeError("render evidence impossible: the scene has no mesh objects")
            report["camera_added"] = scene.camera is None and _frame_camera(scene, meshes)
            scene.render.engine = spec.get("engine") or "BLENDER_WORKBENCH"
            if scene.render.engine == "BLENDER_WORKBENCH":
                scene.display.shading.light = "STUDIO"
                scene.display.shading.color_type = "MATERIAL"
            res = int(spec.get("resolution", 512))
            scene.render.resolution_x = scene.render.resolution_y = res
            scene.render.resolution_percentage = 100
            scene.render.image_settings.file_format = "PNG"
            scene.render.filepath = spec["render"]
            bpy.ops.render.render(write_still=True)
        if spec.get("glb"):
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
