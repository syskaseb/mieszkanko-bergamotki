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

Implementacja: cala geometria budowana w bmesh, nie ma zaleznosci od
kontekstu bpy.ops (w Blender 5.x to bywa kruche w skryptach).
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
    text = path.read_text(encoding="utf-8")
    def grab(attr):
        m = re.search(rf'{attr}="([0-9.]+)\s*mm"', text)
        if not m:
            m = re.search(rf'{attr}="([0-9.]+)"', text)
        return float(m.group(1)) if m else None
    w, h = grab("width"), grab("height")
    if w is None or h is None:
        raise RuntimeError("Nie udalo sie odczytac width/height z SVG")
    return w, h


def import_svg(path):
    bpy.ops.import_curve.svg(filepath=str(path))


def build_combined_bmesh(curves):
    bm = bmesh.new()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for c in curves:
        eval_obj = c.evaluated_get(depsgraph)
        tmp = eval_obj.to_mesh()
        if tmp is None:
            continue
        tmp.transform(c.matrix_world)
        bm.from_mesh(tmp)
        eval_obj.to_mesh_clear()
    return bm


def scale_and_center_bmesh(bm, target_w_m, target_h_m):
    xs = [v.co.x for v in bm.verts]
    ys = [v.co.y for v in bm.verts]
    cur_w = max(xs) - min(xs)
    cur_h = max(ys) - min(ys)
    s = ((target_w_m / cur_w) + (target_h_m / cur_h)) / 2 if cur_w and cur_h else 1.0
    print(f"  skala: x{s:.6f}  (current bbox: {cur_w:.3f} x {cur_h:.3f})")
    cx = (max(xs) + min(xs)) / 2
    cy = (max(ys) + min(ys)) / 2
    for v in bm.verts:
        v.co.x = (v.co.x - cx) * s
        v.co.y = (v.co.y - cy) * s
        v.co.z = v.co.z * s


def get_connected_components(bm):
    bm.edges.index_update()
    visited = set()
    comps = []
    for start in bm.edges:
        if start.index in visited:
            continue
        stack = [start]
        comp = []
        while stack:
            cur = stack.pop()
            if cur.index in visited:
                continue
            visited.add(cur.index)
            comp.append(cur)
            for v in cur.verts:
                for ne in v.link_edges:
                    if ne.index not in visited:
                        stack.append(ne)
        comps.append(comp)
    return comps


def bbox_area(edges):
    xs, ys = [], []
    for e in edges:
        for v in e.verts:
            xs.append(v.co.x); ys.append(v.co.y)
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


def split_outer(bm):
    """Zwraca (outer_bm, inner_bm) — outer = najwiekszy bbox component."""
    comps = get_connected_components(bm)
    comps.sort(key=bbox_area, reverse=True)
    outer_set = {e.index for e in comps[0]}
    print(f"  znaleziono {len(comps)} spojnych komponentow; "
          f"outer ma {len(comps[0])} krawedzi")

    bm.verts.index_update()

    def copy_to(target, edges):
        vmap = {}
        for e in edges:
            v0, v1 = e.verts
            for v in (v0, v1):
                if v.index not in vmap:
                    vmap[v.index] = target.verts.new(v.co)
            try:
                target.edges.new((vmap[v0.index], vmap[v1.index]))
            except ValueError:
                pass  # duplikat krawedzi
        target.verts.index_update()
        target.edges.index_update()

    outer_bm = bmesh.new()
    inner_bm = bmesh.new()
    outer_edges = [e for e in bm.edges if e.index in outer_set]
    inner_edges = [e for e in bm.edges if e.index not in outer_set]
    copy_to(outer_bm, outer_edges)
    copy_to(inner_bm, inner_edges)
    return outer_bm, inner_bm


def extrude_bmesh_up(bm, height):
    edges = list(bm.edges)
    if not edges:
        return
    ret = bmesh.ops.extrude_edge_only(bm, edges=edges)
    new_verts = [g for g in ret["geom"] if isinstance(g, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, vec=(0, 0, height), verts=new_verts)


def make_object_from_bmesh(bm, name):
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def add_solidify(obj, thickness, offset=-1.0):
    mod = obj.modifiers.new(name="Solidify", type="SOLIDIFY")
    mod.thickness = thickness
    mod.offset = offset


def cleanup_orphan_curves():
    for o in list(bpy.data.objects):
        if o.type == "CURVE":
            bpy.data.objects.remove(o, do_unlink=True)


def main():
    reset_scene()

    w_mm, h_mm = parse_svg_dimensions_mm(SVG_PATH)
    target_w = w_mm / 1000.0
    target_h = h_mm / 1000.0
    print(f"SVG viewBox: {w_mm:.1f} x {h_mm:.1f} mm "
          f"-> {target_w:.3f} x {target_h:.3f} m")

    import_svg(SVG_PATH)

    curves = [o for o in bpy.data.objects if o.type == "CURVE"]
    print(f"  zaimportowano {len(curves)} krzywych")
    if not curves:
        raise RuntimeError("SVG nie zaimportowal zadnych krzywych")

    bm = build_combined_bmesh(curves)
    print(f"  bmesh: {len(bm.verts)} v, {len(bm.edges)} e")
    if not bm.verts:
        bm.free()
        raise RuntimeError("Krzywe SVG sa puste")

    scale_and_center_bmesh(bm, target_w, target_h)
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=MERGE_DIST)
    bm.verts.index_update()
    bm.edges.index_update()
    print(f"  po merge: {len(bm.verts)} v, {len(bm.edges)} e")

    outer_bm, inner_bm = split_outer(bm)
    bm.free()

    extrude_bmesh_up(outer_bm, WALL_HEIGHT)
    extrude_bmesh_up(inner_bm, WALL_HEIGHT)

    cleanup_orphan_curves()

    outer_obj = make_object_from_bmesh(outer_bm, "ScianyZewnetrzne")
    inner_obj = make_object_from_bmesh(inner_bm, "ScianyWewnetrzne")
    add_solidify(outer_obj, OUTER_THICK, offset=-1.0)

    print(f"OK — wysokosc {WALL_HEIGHT} m, sciany zewn. {OUTER_THICK*100:.0f} cm")


if __name__ == "__main__":
    main()
