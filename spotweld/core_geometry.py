# SPDX-License-Identifier: GPL-3.0-or-later
"""bmesh-side geometry helpers: island splitting, quad-strip detection and
ordering, ladder (Follow-Active-Quads-style) strip layout, world-oriented
planar projection, and UV application into hotspot rectangles."""

import math

from mathutils import Vector


# ---------------------------------------------------------------------------
# Selection / islands
# ---------------------------------------------------------------------------

def get_target_faces(bm, uv_layer, use_uv_select=False):
    """Selected, visible faces; optionally narrowed to faces whose UVs are
    fully selected (UV editor with sync selection off)."""
    faces = [f for f in bm.faces if f.select and not f.hide]
    if use_uv_select:
        try:
            faces = [f for f in faces if all(l[uv_layer].select for l in f.loops)]
        except AttributeError:
            pass  # selection attribute unavailable — fall back to mesh selection
    return faces


def split_islands(faces, use_seams=True, use_sharp=True, angle_limit=None,
                  use_material=False):
    """Group faces into islands connected across passable edges. Seams, sharp
    edges, over-angle edges, and material boundaries act as island borders."""
    face_set = set(faces)

    def passable(e):
        linked = [f for f in e.link_faces if f in face_set]
        if len(linked) != 2:
            return False
        if use_seams and e.seam:
            return False
        if use_sharp and not e.smooth:
            return False
        if angle_limit is not None and e.calc_face_angle(math.pi) > angle_limit:
            return False
        if use_material and linked[0].material_index != linked[1].material_index:
            return False
        return True

    islands = []
    unvisited = set(faces)
    while unvisited:
        seed = unvisited.pop()
        stack = [seed]
        island = [seed]
        while stack:
            f = stack.pop()
            for e in f.edges:
                if not passable(e):
                    continue
                for other in e.link_faces:
                    if other in unvisited:
                        unvisited.discard(other)
                        stack.append(other)
                        island.append(other)
        islands.append(island)
    return islands


# ---------------------------------------------------------------------------
# Strip detection & ordering
# ---------------------------------------------------------------------------

def _opposite_edge(face, edge):
    """The edge of a quad sharing no verts with `edge` (None for non-quads)."""
    ev = set(edge.verts)
    for e in face.edges:
        if not (set(e.verts) & ev):
            return e
    return None


def detect_strip(faces):
    """Detect whether `faces` form a single unbranched run of quads.

    Returns (ordered_faces, shared_edges, cyclic) or None.
    shared_edges[i] joins ordered[i] and ordered[i+1]; cyclic strips carry a
    final closing edge joining ordered[-1] back to ordered[0]. Mid-strip faces
    must connect through *opposite* quad edges (corner quads reject the strip —
    curved runs are fine, L-turns are not)."""
    if len(faces) < 2:
        return None
    fset = set(faces)
    adj = {}
    for f in faces:
        if len(f.verts) != 4:
            return None
        links = []
        for e in f.edges:
            for other in e.link_faces:
                if other is not f and other in fset:
                    links.append((e, other))
        if len(links) > 2:
            return None  # branching
        adj[f] = links

    ends = [f for f in faces if len(adj[f]) == 1]
    if len(ends) == 2:
        start, cyclic = ends[0], False
    elif not ends and all(len(adj[f]) == 2 for f in faces):
        start, cyclic = faces[0], True
    else:
        return None

    ordered = [start]
    shared = []
    visited = {start}
    cur, prev_edge = start, None
    while len(ordered) < len(faces):
        step = None
        for e, other in adj[cur]:
            if e is not prev_edge and other not in visited:
                step = (e, other)
                break
        if step is None:
            return None  # stalled — not a single run
        shared.append(step[0])
        ordered.append(step[1])
        visited.add(step[1])
        prev_edge, cur = step
    if cyclic:
        closing = None
        for e, other in adj[ordered[-1]]:
            if other is start and e is not prev_edge:
                closing = e
                break
        if closing is None:
            return None
        shared.append(closing)

    # mid faces must be crossed via opposite edges
    m = len(ordered)
    for i in range(m):
        e_in = shared[i - 1] if (cyclic or i > 0) else None
        e_out = shared[i] if (cyclic or i < m - 1) else None
        if e_in is not None and e_out is not None:
            if _opposite_edge(ordered[i], e_in) is not e_out:
                return None
    return ordered, shared, cyclic


# ---------------------------------------------------------------------------
# Strip layout (ladder parametrization — Follow Active Quads, Length Average)
# ---------------------------------------------------------------------------

class StripLayout:
    __slots__ = ("faces", "loop_uv", "total_len", "avg_width", "cyclic")


def layout_strip(ordered, shared, cyclic, mw, world_orient=True):
    """Parametrize an ordered quad strip: u accumulates world length along the
    run (per-quad texel density preserved), v spans 0..1 across the rungs.
    Returns StripLayout with loop -> (u_world, v_norm), or None if degenerate."""
    m = len(ordered)

    def wco(v):
        return mw @ v.co

    face_data = []  # (face, left_pair, right_pair)
    us = [0.0]
    widths = []

    left_edge = shared[-1] if cyclic else _opposite_edge(ordered[0], shared[0])
    if left_edge is None:
        return None
    left = (left_edge.verts[0], left_edge.verts[1])

    for i, f in enumerate(ordered):
        if cyclic:
            r_edge = shared[i]
        else:
            r_edge = shared[i] if i < m - 1 else _opposite_edge(f, shared[-1])
        if r_edge is None:
            return None
        x, y = r_edge.verts
        a_next = None
        for e in f.edges:
            sv = set(e.verts)
            if left[0] in sv and x in sv:
                a_next = x
                break
            if left[0] in sv and y in sv:
                a_next = y
                break
        if a_next is None:  # degenerate — pick nearer vert
            a_next = x if (wco(x) - wco(left[0])).length <= (wco(y) - wco(left[0])).length else y
        right = (a_next, y if a_next is x else x)

        face_data.append((f, left, right))
        widths.append((wco(left[0]) - wco(left[1])).length)
        adv = ((wco(right[0]) - wco(left[0])).length +
               (wco(right[1]) - wco(left[1])).length) * 0.5
        us.append(us[-1] + adv)
        left = right
    widths.append((wco(face_data[-1][2][0]) - wco(face_data[-1][2][1])).length)

    avg_width = max(sum(widths) / len(widths), 1e-9)
    total_len = max(us[-1], 1e-9)

    flip_v = False
    if world_orient:
        za = sum(wco(fd[1][0]).z for fd in face_data) + wco(face_data[-1][2][0]).z
        zb = sum(wco(fd[1][1]).z for fd in face_data) + wco(face_data[-1][2][1]).z
        flip_v = za > zb  # higher side maps to v=1

    loop_uv = {}
    for i, (f, lpair, rpair) in enumerate(face_data):
        u_l, u_r = us[i], us[i + 1]
        v0, v1 = (1.0, 0.0) if flip_v else (0.0, 1.0)
        vmap = {lpair[0]: (u_l, v0), lpair[1]: (u_l, v1),
                rpair[0]: (u_r, v0), rpair[1]: (u_r, v1)}
        for l in f.loops:
            if l.vert not in vmap:
                return None  # twisted/degenerate quad
            loop_uv[l] = vmap[l.vert]

    sl = StripLayout()
    sl.faces = ordered
    sl.loop_uv = loop_uv
    sl.total_len = total_len
    sl.avg_width = avg_width
    sl.cyclic = cyclic
    return sl


def apply_rect_to_strip(strip, uv_layer, rect, inset_uv=(0.0, 0.0), rotated=False,
                        snap_tiles=True, reverse_u=False, tex_aspect=1.0):
    """Map a StripLayout into `rect`: cross-section fills the rect's band
    dimension, length tiles along the other axis at matching texel density.
    snap_tiles nudges the scale so a whole number of rect-widths covers the
    run; `tex_aspect` (tex_height / tex_width) keeps the density square on
    non-square textures."""
    iu, iv = inset_uv
    if rotated:
        band = (rect.umin + iu, rect.umax - iu)
        tile = (rect.vmin + iv, rect.vmax - iv)
    else:
        band = (rect.vmin + iv, rect.vmax - iv)
        tile = (rect.umin + iu, rect.umax - iu)
    band_h = max(band[1] - band[0], 1e-6)
    tile_w = max(tile[1] - tile[0], 1e-6)

    # One UV unit spans different px counts on U and V of a non-square
    # texture; convert the band-axis density to the tiling axis.
    tex_aspect = max(tex_aspect, 1e-9)
    uv_per_world = (band_h / strip.avg_width) * \
        (tex_aspect if not rotated else 1.0 / tex_aspect)
    total_uv = strip.total_len * uv_per_world
    if snap_tiles and total_uv > 1e-9:
        k = max(1, round(total_uv / tile_w))
        uv_per_world *= (k * tile_w) / total_uv
        total_uv = strip.total_len * uv_per_world

    for loop, (u, vs) in strip.loop_uv.items():
        uu = u * uv_per_world
        if reverse_u:
            uu = total_uv - uu
        tpos = tile[0] + uu
        bpos = band[0] + vs * band_h
        loop[uv_layer].uv = (bpos, tpos) if rotated else (tpos, bpos)


# ---------------------------------------------------------------------------
# Island projection & fit
# ---------------------------------------------------------------------------

def island_projection(faces, mw):
    """World-oriented planar projection of an island onto its average normal
    plane (world up projects to +Y of the plane basis).
    Returns (loop -> (x, y) dict, (minx, miny, width, height))."""
    # Normals transform by the inverse-transpose, not the plain 3x3 —
    # otherwise non-uniform object scale tilts the projection plane.
    n3 = mw.to_3x3().inverted_safe().transposed()
    nrm = Vector((0.0, 0.0, 0.0))
    for f in faces:
        nrm += (n3 @ f.normal) * f.calc_area()
    if nrm.length < 1e-9:
        nrm = Vector((0.0, 0.0, 1.0))
    nrm.normalize()
    up = Vector((0.0, 0.0, 1.0)) if abs(nrm.z) < 0.999 else Vector((0.0, 1.0, 0.0))
    x_axis = up.cross(nrm)
    if x_axis.length < 1e-9:
        x_axis = Vector((1.0, 0.0, 0.0))
    x_axis.normalize()
    y_axis = nrm.cross(x_axis)

    coords = {}
    xs, ys = [], []
    for f in faces:
        for l in f.loops:
            co = mw @ l.vert.co
            x, y = co.dot(x_axis), co.dot(y_axis)
            coords[l] = (x, y)
            xs.append(x)
            ys.append(y)
    minx, miny = min(xs), min(ys)
    w = max(max(xs) - minx, 1e-9)
    h = max(max(ys) - miny, 1e-9)
    return coords, (minx, miny, w, h)


def face_world_area(f, mw):
    vs = [mw @ v.co for v in f.verts]
    area = 0.0
    for i in range(1, len(vs) - 1):
        area += (vs[i] - vs[0]).cross(vs[i + 1] - vs[0]).length * 0.5
    return area


def rectify_quad_normalized(face, coords):
    """Exact unit-square corners for a lone quad, honoring projected winding
    and orientation, so skewed quads map corner-to-corner without smearing."""
    loops = list(face.loops)
    pts = [coords[l] for l in loops]
    area2 = 0.0
    for i in range(4):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % 4]
        area2 += x0 * y1 - x1 * y0
    order = loops if area2 >= 0.0 else loops[::-1]
    pts_o = [coords[l] for l in order]
    start = min(range(4), key=lambda i: pts_o[i][0] + pts_o[i][1])
    corners = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    return {order[(start + k) % 4]: corners[k] for k in range(4)}


def apply_rect_to_island(faces, coords, bbox, uv_layer, rect,
                         inset_uv=(0.0, 0.0), rot_quarters=0, mirror_u=False):
    """Normalize projected island coords to 0..1 and map into `rect`.
    rot_quarters applies 90° steps (proper rotations, never accidental
    mirrors); mirror_u flips U afterwards."""
    minx, miny, w, h = bbox
    normalized = None
    if len(faces) == 1 and len(faces[0].verts) == 4:
        normalized = rectify_quad_normalized(faces[0], coords)

    iu, iv = inset_uv
    u0 = rect.umin + iu
    v0 = rect.vmin + iv
    uw = max(rect.umax - iu - u0, 1e-6)
    vh = max(rect.vmax - iv - v0, 1e-6)
    for f in faces:
        for l in f.loops:
            if normalized is not None:
                nx, ny = normalized[l]
            else:
                x, y = coords[l]
                nx, ny = (x - minx) / w, (y - miny) / h
            for _ in range(rot_quarters % 4):
                nx, ny = ny, 1.0 - nx
            if mirror_u:
                nx = 1.0 - nx
            l[uv_layer].uv = (u0 + nx * uw, v0 + ny * vh)


# ---------------------------------------------------------------------------
# UV backup / restore, strip growing
# ---------------------------------------------------------------------------

def backup_uvs(faces, uv_layer):
    return [(l, tuple(l[uv_layer].uv)) for f in faces for l in f.loops]


def restore_uvs(backup, uv_layer):
    for l, uv in backup:
        l[uv_layer].uv = uv


def walk_run(face, edge, delimit):
    """Faces reachable from `face` by crossing `edge` and continuing through
    opposite quad edges until a delimiter, non-quad, or branch. Returns the
    faces beyond `face` in walk order."""
    out = []
    visited = {face}
    cur_f, cur_e = face, edge
    while cur_e is not None and not delimit(cur_e):
        nxt = None
        for other in cur_e.link_faces:
            if other is not cur_f and other not in visited and not other.hide \
                    and len(other.verts) == 4:
                nxt = other
                break
        if nxt is None:
            break
        out.append(nxt)
        visited.add(nxt)
        cur_f, cur_e = nxt, _opposite_edge(nxt, cur_e)
        if len(out) > 100000:
            break
    return out
