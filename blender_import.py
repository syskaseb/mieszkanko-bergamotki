"""
Import rzutu 2D (DXF z FreeCAD) do Blendera i ekstruzja ścian 3D.

Uruchomienie:
    1. Otwórz Blender
    2. Workspace "Scripting" -> Open -> blender_import.py
    3. Run Script (Alt+P)

Co robi:
    - czyści scenę
    - importuje rzut_2d_draft.dxf
    - skaluje mm -> m
    - znajduje największy zamknięty obrys (= ściany zewnętrzne) i daje
      mu grubość 35 cm + wysokość 3 m
    - resztę linii (ściany wewnętrzne, które masz narysowane parami)
      wyciąga pionowo do 3 m jako płaszczyzny
"""

import bpy
import bmesh
import addon_utils
from pathlib import Path

# ---------------- KONFIG ----------------
DXF_PATH = Path(r"G:\My Drive\Bergamotki urządzanie\mieszkanko-bergamotki\rzut_2d_draft.dxf")
WALL_HEIGHT = 3.0       # m
OUTER_THICK = 0.35      # m
SCALE = 0.001           # mm -> m
# ----------------------------------------


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def enable_dxf_addon():
    for name in ("io_import_dxf", "io_scene_dxf"):
        try:
            addon_utils.enable(name, default_set=True, persistent=True)
        except Exception:
            pass


def import_dxf(path):
    if not path.exists():
        raise FileNotFoundError(path)
    bpy.ops.import_scene.dxf(filepath=str(path))


def join_to_single_mesh(name="Plan2D"):
    objs = [o for o in bpy.context.scene.objects if o.type in {"CURVE", "MESH"}]
    if not objs:
        raise RuntimeError("Nic nie zaimportowano z DXF")

    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
        bpy.context.view_layer.objects.active = o
        if o.type == "CURVE":
            bpy.ops.object.convert(target="MESH")

    bpy.ops.object.select_all(action="DESELECT")
    for o in bpy.context.scene.objects:
        if o.type == "MESH":
            o.select_set(True)
            bpy.context.view_layer.objects.active = o
    bpy.ops.object.join()

    obj = bpy.context.active_object
    obj.name = name
    return obj


def scale_and_center(obj):
    obj.scale = (SCALE, SCALE, SCALE)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    obj.location = (0, 0, 0)


def find_largest_component_edges(obj):
    """Zwraca zbiór indeksów krawędzi największego (po bbox 2D)
    spójnego komponentu — heurystyka na zewnętrzny obrys."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)

    visited = set()
    components = []
    for start in bm.edges:
        if start.index in visited:
            continue
        stack = [start]
        comp = []
        while stack:
            e = stack.pop()
            if e.index in visited:
                continue
            visited.add(e.index)
            comp.append(e)
            for v in e.verts:
                for ne in v.link_edges:
                    if ne.index not in visited:
                        stack.append(ne)
        components.append(comp)

    def bbox_area(edges):
        xs, ys = [], []
        for e in edges:
            for v in e.verts:
                xs.append(v.co.x); ys.append(v.co.y)
        return (max(xs) - min(xs)) * (max(ys) - min(ys))

    components.sort(key=bbox_area, reverse=True)
    outer_idx = {e.index for e in components[0]}
    bm.free()
    return outer_idx


def separate_edges(obj, edge_indices, new_name):
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="OBJECT")
    for e in obj.data.edges:
        e.select = e.index in edge_indices
    for v in obj.data.vertices:
        v.select = False
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.separate(type="SELECTED")
    bpy.ops.object.mode_set(mode="OBJECT")

    new_obj = [o for o in bpy.context.selected_objects if o is not obj][0]
    new_obj.name = new_name
    return new_obj


def extrude_up(obj, height):
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.extrude_edges_move(
        TRANSFORM_OT_translate={"value": (0, 0, height)}
    )
    bpy.ops.object.mode_set(mode="OBJECT")


def add_solidify(obj, thickness, offset=-1.0):
    mod = obj.modifiers.new(name="Solidify", type="SOLIDIFY")
    mod.thickness = thickness
    mod.offset = offset


def main():
    reset_scene()
    enable_dxf_addon()
    import_dxf(DXF_PATH)

    plan = join_to_single_mesh("Plan2D")
    scale_and_center(plan)

    outer_edges = find_largest_component_edges(plan)
    outer = separate_edges(plan, outer_edges, "ScianyZewnetrzne")
    plan.name = "ScianyWewnetrzne"

    extrude_up(plan, WALL_HEIGHT)
    extrude_up(outer, WALL_HEIGHT)
    add_solidify(outer, OUTER_THICK, offset=-1.0)

    print(f"OK — wysokosc {WALL_HEIGHT} m, sciany zewn. {OUTER_THICK*100:.0f} cm")


if __name__ == "__main__":
    main()
