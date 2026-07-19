# SPDX-License-Identifier: GPL-3.0-or-later
"""Predictive atlas suggestion: cluster measured mesh patches into reusable
rectangle buckets and shelf-pack them into a texture layout.

No bpy imports — unit-testable outside Blender.

Packing note: the plan proposed reusing bpy.ops.uv.pack_islands on placeholder
geometry, but every suggested region is an axis-aligned rectangle (strips are
full-width bands by convention), so a deterministic shelf packer is both
simpler and avoids temp-object/mode juggling entirely.
"""

import math

PRESET_TOLERANCE = {
    "LEAN": 0.30,      # favors reuse — fewest rectangles, most repetition
    "BALANCED": 0.15,  # recommended default for modular environments
    "HIFI": 0.05,      # near one rectangle per unique patch
}


class Patch:
    """One measured mesh patch. Islands: long/short = oriented bbox sides
    (long >= short), area = filled world area. Strips: short = cross-section
    width, length = run length (irrelevant for bucketing — strips tile)."""

    __slots__ = ("kind", "long", "short", "area", "length")

    def __init__(self, kind, long_side, short_side, area=0.0, length=0.0):
        self.kind = kind  # 'ISLAND' | 'STRIP'
        self.long = max(long_side, 1e-9)
        self.short = max(short_side, 1e-9)
        self.area = area
        self.length = length


class Bucket:
    __slots__ = ("kind", "long", "short", "members")

    def __init__(self, kind, long_side, short_side, members):
        self.kind = kind
        self.long = long_side
        self.short = short_side
        self.members = members


def _close(a, b, tol):
    """Relative closeness: within a factor of (1 + tol)."""
    return abs(math.log(max(a, 1e-9)) - math.log(max(b, 1e-9))) <= math.log(1.0 + tol)


def cluster_patches(patches, tol):
    """Greedy similarity clustering, largest patches first. Strips bucket by
    cross-width only; islands need both sides within tolerance (which bounds
    both aspect and size drift). Bucket dims track the running member mean."""
    strips = sorted((p for p in patches if p.kind == "STRIP"), key=lambda p: -p.short)
    islands = sorted((p for p in patches if p.kind == "ISLAND"),
                     key=lambda p: -(p.long * p.short))
    buckets = []

    for p in strips:
        target = next((b for b in buckets
                       if b.kind == "STRIP" and _close(b.short, p.short, tol)), None)
        if target is None:
            buckets.append(Bucket("STRIP", 0.0, p.short, [p]))
        else:
            target.members.append(p)
            target.short = sum(m.short for m in target.members) / len(target.members)

    for p in islands:
        target = next((b for b in buckets
                       if b.kind == "ISLAND"
                       and _close(b.long, p.long, tol)
                       and _close(b.short, p.short, tol)), None)
        if target is None:
            buckets.append(Bucket("ISLAND", p.long, p.short, [p]))
        else:
            target.members.append(p)
            n = len(target.members)
            target.long = sum(m.long for m in target.members) / n
            target.short = sum(m.short for m in target.members) / n

    return buckets


def size_buckets(buckets, texel_density, tex_w, tex_h):
    """Pixel dimensions per bucket at the target texel density (px per world
    unit). Strips become full-width bands. Returns [(bucket, w_px, h_px,
    full_width)], plus a flag when anything had to clamp to the texture."""
    sized = []
    clamped = False
    for b in buckets:
        if b.kind == "STRIP":
            h = int(round(b.short * texel_density))
            hc = max(2, min(h, tex_h))
            clamped |= hc != h
            sized.append((b, tex_w, hc, True))
        else:
            w = int(round(b.long * texel_density))
            h = int(round(b.short * texel_density))
            wc = max(2, min(w, tex_w))
            hc = max(2, min(h, tex_h))
            clamped |= wc != w or hc != h
            sized.append((b, wc, hc, False))
    return sized, clamped


def shelf_pack(sized, tex_w, tex_h, pad):
    """Deterministic shelf packing: full-width strip bands stack from the
    bottom, islands fill left-to-right shelves above, tallest first.
    Returns ([(bucket, w, h, full_width, x, y)] with bottom-left px origin,
    used_height_px)."""
    placements = []
    y = 0
    strips = [s for s in sized if s[3]]
    islands = sorted((s for s in sized if not s[3]), key=lambda s: (-s[2], -s[1]))

    for b, w, h, _full in strips:
        placements.append((b, tex_w, h, True, 0, y))
        y += h + pad

    shelf_y, shelf_h, x = y, 0, 0
    for b, w, h, _full in islands:
        if x > 0 and x + w > tex_w:
            shelf_y += shelf_h + pad
            x, shelf_h = 0, 0
        placements.append((b, w, h, False, x, shelf_y))
        x += w + pad
        shelf_h = max(shelf_h, h)

    if islands:
        used = shelf_y + shelf_h
    else:
        used = max(0, y - pad)
    return placements, used


# ---------------------------------------------------------------------------
# Density-free full-coverage packing (power-of-two trim-sheet layout)
# ---------------------------------------------------------------------------

def _pow2_round(v, lo, hi):
    """Nearest power of two to v in log space, clamped to [lo, hi]."""
    v = max(float(v), 1.0)
    p = 2.0 ** round(math.log(v, 2.0))
    return int(min(max(p, lo), hi))


def _pow2_pieces(total):
    """Exact descending power-of-two decomposition (binary digits), so any
    leftover span partitions into clean po2 filler pieces."""
    out = []
    total = int(total)
    while total > 0:
        p = 1 << (total.bit_length() - 1)
        out.append(p)
        total -= p
    return out


def pack_full_atlas(buckets, tex_w, tex_h, min_px=8):
    """Density-free layout: every bucket gets power-of-two pixel sizes, the
    scale is chosen so the measured rects fill as much of the texture as
    possible, and the exact remainder is partitioned into filler cells (per
    row) and full-width trim bands — bucket None marks fillers. The result
    covers the whole texture with no overlaps and no padding.

    Returns (placements, used_h, px_per_world) — shelf_pack's (placements,
    used_h) plus the derived scale. used_h > tex_h signals overflow even at
    minimum sizes (fillers omitted)."""
    strips = [b for b in buckets if b.kind == "STRIP"]
    islands = [b for b in buckets if b.kind == "ISLAND"]

    def sizes_at(s):
        band_hs = [(b, _pow2_round(b.short * s, min_px, tex_h)) for b in strips]
        cells = [(b, _pow2_round(b.long * s, min_px, tex_w),
                  _pow2_round(b.short * s, min_px, tex_h)) for b in islands]
        cells.sort(key=lambda t: (-t[2], -t[1]))
        return band_hs, cells

    def rows_of(cells):
        """Greedy rows of equal po2 height (row order follows the tallest-
        first cell order). Returns [[row_h, [(b, w, h), ...], used_w], ...]."""
        rows = []
        for b, w, h in cells:
            for row in rows:
                if row[0] == h and row[2] + w <= tex_w:
                    row[1].append((b, w, h))
                    row[2] += w
                    break
            else:
                rows.append([h, [(b, w, h)], w])
        return rows

    def used_height(s):
        band_hs, cells = sizes_at(s)
        return (sum(h for _b, h in band_hs)
                + sum(r[0] for r in rows_of(cells)))

    def build(s, fillers):
        """Emit placements at scale `s`: strip bands, then equal-height cell
        rows. With `fillers`, the exact remainder of each row and the trailing
        vertical span become filler placements (bucket None)."""
        band_hs, cells = sizes_at(s)
        placements = []
        y = 0
        for b, h in band_hs:
            placements.append((b, tex_w, h, True, 0, y))
            y += h
        for row_h, row_cells, _w in rows_of(cells):
            x = 0
            for b, w, h in row_cells:
                placements.append((b, w, h, False, x, y))
                x += w
            if fillers:
                for piece in _pow2_pieces(tex_w - x):
                    placements.append((None, piece, row_h, False, x, y))
                    x += piece
            y += row_h
        used = y
        if fillers:
            for piece in _pow2_pieces(tex_h - y):
                placements.append((None, tex_w, piece, True, 0, y))
                y += piece
        return placements, used

    # Largest scale whose layout still fits the texture height. used_height
    # is stepwise (po2 snapping) but non-decreasing overall; the search keeps
    # `lo` on the last known-fitting scale.
    dims = [b.short for b in strips]
    for b in islands:
        dims += [b.long, b.short]
    smallest = max(min(dims), 1e-9)
    lo = 1e-9  # everything clamps to min_px
    if used_height(lo) > tex_h:
        placements, used = build(lo, fillers=False)
        return placements, used, lo
    hi = 2.0 * max(tex_w, tex_h) / smallest  # smallest patch spans the atlas
    for _ in range(64):
        mid = 0.5 * (lo + hi)
        if used_height(mid) <= tex_h:
            lo = mid
        else:
            hi = mid

    placements, used = build(lo, fillers=True)
    return placements, used, lo
