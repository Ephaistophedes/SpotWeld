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
