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


def split_outer_edges(bm):
    """Zwraca (outer_edges, inner_edges) - listy BMEdge z bm."""
    comps = get_connected_components(bm)
    comps.sort(key=bbox_area, reverse=True)
    outer_set = {e.index for e in comps[0]}
    print(f"  znaleziono {len(comps)} spojnych komponentow; "
          f"outer ma {len(comps[0])} krawedzi")
    outer = [e for e in bm.edges if e.index in outer_set]
    inner = [e for e in bm.edges if e.index not in outer_set]
    return outer, inner


def extract_loop_2d(edges):
    """Z listy krawedzi tworzacych zamkniety pierscien wyciaga kolejne (x,y)
    w kolejnosci wokol obwodu. Zwraca [] jesli nie da sie domknac."""
    if not edges:
        return []
    # zbuduj graf
    adj = {}
    for e in edges:
        v0, v1 = e.verts
        adj.setdefault(v0, []).append(v1)
        adj.setdefault(v1, []).append(v0)

    # sprawdz ze wszystkie wierzcholki maja dokladnie 2 sasiadow (czysta petla)
    bad = [v for v, ns in adj.items() if len(ns) != 2]
    if bad:
        print(f"  UWAGA: outer ma {len(bad)} wierzcholkow nie-2-stopniowych")
        return []

    start = next(iter(adj))
    loop = [start]
    prev = None
    cur = start
    while True:
        nbrs = adj[cur]
        nxt = nbrs[0] if nbrs[0] != prev else nbrs[1]
        if nxt == start:
            break
        loop.append(nxt)
        prev = cur
        cur = nxt
        if len(loop) > len(adj) + 1:
            return []  # cos sie zaplatalo
    return [(v.co.x, v.co.y) for v in loop]


def signed_area_2d(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += (x2 - x1) * (y2 + y1)
    return s


def offset_polygon_inward(pts, offset):
    """2D offset zamknietej polilinii o `offset` do wewnatrz.
    Dziala dla dowolnych pętli (nie tylko prostokątnych)."""
    n = len(pts)
    ccw = signed_area_2d(pts) < 0  # CCW = ujemne pole w naszej konwencji
    # zbuduj offsetowane segmenty
    offsets = []
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        L = (dx * dx + dy * dy) ** 0.5
        if L == 0:
            offsets.append(((x1, y1), (x2, y2)))
            continue
        if ccw:
            nx, ny = -dy / L, dx / L
        else:
            nx, ny = dy / L, -dx / L
        offsets.append(((x1 + nx * offset, y1 + ny * offset),
                        (x2 + nx * offset, y2 + ny * offset)))

    # przeciecia kolejnych offsetowanych segmentow
    out = []
    for i in range(n):
        p1, p2 = offsets[(i - 1) % n]
        p3, p4 = offsets[i]
        x1, y1 = p1; x2, y2 = p2
        x3, y3 = p3; x4, y4 = p4
        denom = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
        if abs(denom) < 1e-9:
            out.append(p2)
        else:
            t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / denom
            out.append((x1 + t * (x2 - x1), y1 + t * (y2 - y1)))
    return out


def build_outer_wall_bmesh(outer_2d, thickness, height):
    """Buduje pelny prostopadloscienny pierscien sciany (outer + inner kontur,
    podloga, sufit, oba lica) — wynikowo wodoszczelny solid."""
    inner_2d = offset_polygon_inward(outer_2d, thickness)

    bm = bmesh.new()
    n = len(outer_2d)
    bo = [bm.verts.new((x, y, 0)) for x, y in outer_2d]
    bi = [bm.verts.new((x, y, 0)) for x, y in inner_2d]
    to = [bm.verts.new((x, y, height)) for x, y in outer_2d]
    ti = [bm.verts.new((x, y, height)) for x, y in inner_2d]

    for i in range(n):
        j = (i + 1) % n
        # zewnetrzne lico
        bm.faces.new([bo[i], bo[j], to[j], to[i]])
        # wewnetrzne lico (odwrotna kolejnosc — normalna do srodka)
        bm.faces.new([bi[j], bi[i], ti[i], ti[j]])
        # gora (cap)
        bm.faces.new([to[i], to[j], ti[j], ti[i]])
        # dol
        bm.faces.new([bi[i], bi[j], bo[j], bo[i]])

    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    return bm


def build_inner_walls_bmesh(inner_edges, height):
    """Wszystkie pozostale linie - extrude w gore jako pionowe plaszczyzny."""
    bm = bmesh.new()
    vmap = {}
    for e in inner_edges:
        v0, v1 = e.verts
        for v in (v0, v1):
            if v.index not in vmap:
                vmap[v.index] = bm.verts.new(v.co)
        try:
            bm.edges.new((vmap[v0.index], vmap[v1.index]))
        except ValueError:
            pass
    bm.verts.index_update()
    bm.edges.index_update()
    edges = list(bm.edges)
    if edges:
        ret = bmesh.ops.extrude_edge_only(bm, edges=edges)
        new_verts = [g for g in ret["geom"] if isinstance(g, bmesh.types.BMVert)]
        bmesh.ops.translate(bm, vec=(0, 0, height), verts=new_verts)
    return bm


def make_object_from_bmesh(bm, name):
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def add_solidify(obj, thickness, offset=0.0):
    mod = obj.modifiers.new(name="Solidify", type="SOLIDIFY")
    mod.thickness = thickness
    mod.offset = offset
    mod.use_even_offset = True


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

    outer_edges, inner_edges = split_outer_edges(bm)

    outer_2d = extract_loop_2d(outer_edges)
    cleanup_orphan_curves()

    if outer_2d:
        print(f"  outer loop: {len(outer_2d)} wierzcholkow — buduje solid")
        outer_bm = build_outer_wall_bmesh(outer_2d, OUTER_THICK, WALL_HEIGHT)
        outer_obj = make_object_from_bmesh(outer_bm, "ScianyZewnetrzne")
    else:
        print("  outer nie domknal sie — fallback na ribbon + Solidify")
        outer_bm = build_inner_walls_bmesh(outer_edges, WALL_HEIGHT)
        outer_obj = make_object_from_bmesh(outer_bm, "ScianyZewnetrzne")
        add_solidify(outer_obj, OUTER_THICK, offset=-1.0)

    inner_bm = build_inner_walls_bmesh(inner_edges, WALL_HEIGHT)
    inner_obj = make_object_from_bmesh(inner_bm, "ScianyWewnetrzne")
    # delikatna grubosc na wewn. plaszczyznach zeby byly widoczne jako 3D
    add_solidify(inner_obj, 0.05, offset=0.0)

    bm.free()
    print(f"OK — wysokosc {WALL_HEIGHT} m, sciany zewn. {OUTER_THICK*100:.0f} cm")


if __name__ == "__main__":
    main()
