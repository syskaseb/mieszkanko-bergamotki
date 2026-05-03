"""
Import rzutu 2D (SVG z FreeCAD - Flattened SVG) do Blendera i ekstruzja scian 3D.

Uruchomienie:
    1. Otworz Blender (5.x ok)
    2. Workspace "Scripting" -> Open -> blender_import.py
    3. Run Script (Alt+P)

Co robi:
    - czysci scene
    - importuje rzut_2d_draft.svg
    - skaluje do metrow na podstawie atrybutow width/height z SVG
    - znajduje najwiekszy zamkniety obrys (= sciany zewnetrzne) i daje
      mu grubosc 35 cm + wysokosc 3 m
    - reszte linii (sciany wewnetrzne narysowane parami + linie konstrukcyjne)
      wyciaga pionowo do 3 m jako plaszczyzny
"""

import re
import bpy
import bmesh
from pathlib import Path

# ---------------- KONFIG ----------------
SVG_PATH = Path(r"G:\My Drive\Bergamotki urządzanie\mieszkanko-bergamotki\rzut_2d_draft.svg")
WALL_HEIGHT = 3.0       # m
OUTER_THICK = 0.35      # m
MERGE_DIST = 0.005      # m — scalanie wierzcholkow na stykach
# ----------------------------------------


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def parse_svg_dimensions_mm(path):
    """Wyciaga width/height z SVG, zwraca (w_mm, h_mm)."""
    text = path.read_text(encoding="utf-8")
    def grab(attr):
        m = re.search(rf'{attr}="([0-9.]+)\s*mm"', text)
        if not m:
            m = re.search(rf'{attr}="([0-9.]+)"', text)
        return float(m.group(1)) if m else None
    w = grab("width")
    h = grab("height")
    if w is None or h is None:
        raise RuntimeError("Nie udalo sie odczytac width/height z SVG")
    return w, h


def import_svg(path):
    bpy.ops.import_curve.svg(filepath=str(path))


def relink_to_scene_root(objs):
    """Przerzuca obiekty do glownej collection sceny (zeby byly widoczne
    dla view_layer i operatorow bpy.ops)."""
    root = bpy.context.scene.collection
    for o in objs:
        for coll in list(o.users_collection):
            try:
                coll.objects.unlink(o)
            except RuntimeError:
                pass
        if o.name not in root.objects:
            root.objects.link(o)


def join_to_single_mesh(name="Plan2D"):
    """Konwertuje wszystkie krzywe do jednego mesha bez uzywania bpy.ops
    (w Blender 5.x convert operator bywa zawodny w kontekscie skryptu)."""
    curves = [o for o in bpy.data.objects if o.type == "CURVE"]
    print(f"  znaleziono {len(curves)} krzywych w SVG")
    if not curves:
        raise RuntimeError("SVG nie zaimportowal zadnych krzywych")

    combined = bmesh.new()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for c in curves:
        eval_obj = c.evaluated_get(depsgraph)
        tmp_mesh = eval_obj.to_mesh()
        if tmp_mesh is None:
            continue
        # transform do world space
        tmp_mesh.transform(c.matrix_world)
        combined.from_mesh(tmp_mesh)
        eval_obj.to_mesh_clear()

    print(f"  zbudowany bmesh: {len(combined.verts)} v, {len(combined.edges)} e")
    if len(combined.verts) == 0:
        combined.free()
        raise RuntimeError("Krzywe SVG sa puste — brak geometrii do ekstruzji")

    mesh_data = bpy.data.meshes.new(name)
    combined.to_mesh(mesh_data)
    combined.free()

    obj = bpy.data.objects.new(name, mesh_data)
    bpy.context.scene.collection.objects.link(obj)

    # usun stare krzywe
    for c in curves:
        bpy.data.objects.remove(c, do_unlink=True)

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


def rescale_to_meters(obj, target_w_m, target_h_m):
    """Mierzy obecny bbox (w lokalnych jednostkach Blendera po imporcie SVG)
    i skaluje tak, zeby zgadzal sie z target w metrach."""
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    coords = [v.co for v in obj.data.vertices]
    xs = [c.x for c in coords]
    ys = [c.y for c in coords]
    cur_w = max(xs) - min(xs)
    cur_h = max(ys) - min(ys)
    sx = target_w_m / cur_w if cur_w else 1.0
    sy = target_h_m / cur_h if cur_h else 1.0
    s = (sx + sy) / 2.0  # jednolita skala (zachowac proporcje)
    obj.scale = (s, s, s)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)


def merge_doubles(obj, dist):
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=dist)
    bpy.ops.object.mode_set(mode="OBJECT")


def center_origin(obj):
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    obj.location = (0, 0, 0)


def find_largest_component_edges(obj):
    """Indeksy krawedzi najwiekszego (po bbox 2D) spojnego komponentu."""
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
    for v in obj.data.vertices:
        v.select = False
    for e in obj.data.edges:
        e.select = e.index in edge_indices
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.separate(type="SELECTED")
    bpy.ops.object.mode_set(mode="OBJECT")
    new_obj = [o for o in bpy.context.selected_objects if o is not obj][-1]
    new_obj.name = new_name
    return new_obj


def extrude_up(obj, height):
    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = obj
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

    target_w_mm, target_h_mm = parse_svg_dimensions_mm(SVG_PATH)
    target_w_m = target_w_mm / 1000.0
    target_h_m = target_h_mm / 1000.0
    print(f"SVG viewBox: {target_w_mm:.1f} x {target_h_mm:.1f} mm "
          f"-> {target_w_m:.3f} x {target_h_m:.3f} m")

    import_svg(SVG_PATH)
    plan = join_to_single_mesh("Plan2D")
    rescale_to_meters(plan, target_w_m, target_h_m)
    merge_doubles(plan, MERGE_DIST)
    center_origin(plan)

    outer_edges = find_largest_component_edges(plan)
    outer = separate_edges(plan, outer_edges, "ScianyZewnetrzne")
    plan.name = "ScianyWewnetrzne"

    extrude_up(plan, WALL_HEIGHT)
    extrude_up(outer, WALL_HEIGHT)
    add_solidify(outer, OUTER_THICK, offset=-1.0)

    print(f"OK — wysokosc {WALL_HEIGHT} m, sciany zewn. {OUTER_THICK*100:.0f} cm")


if __name__ == "__main__":
    main()
