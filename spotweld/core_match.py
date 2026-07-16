# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-Python hotspot rectangle matching and Valve .rect KeyValues I/O.

No bpy imports — unit-testable outside Blender.

Coordinate conventions:
- Internally rects are normalized UV (0..1), origin bottom-left (Blender).
- .rect files are pixels, origin top-left (Valve). Y flips on import/export.
"""

import math
import re
from collections import namedtuple

FULL_WIDTH_TOL = 1.0 / 8192.0


def quantize_aspect(aspect):
    """DreamUV-style aspect quantization: wide ratios round to whole numbers,
    tall ratios to whole-number reciprocals (2.3 -> 2.0, 0.4 -> 0.5)."""
    if aspect <= 0.0:
        return 1.0
    if aspect >= 1.0:
        return float(max(1, round(aspect)))
    return 1.0 / max(1, round(1.0 / aspect))


class Rect:
    """One hotspot rectangle, normalized 0..1 UV, bottom-left origin."""

    __slots__ = ("umin", "vmin", "umax", "vmax", "rotate", "reflect", "alt", "tiling", "name")

    def __init__(self, umin, vmin, umax, vmax,
                 rotate=False, reflect=False, alt=False, tiling=False, name=""):
        self.umin, self.umax = (umin, umax) if umin <= umax else (umax, umin)
        self.vmin, self.vmax = (vmin, vmax) if vmin <= vmax else (vmax, vmin)
        self.rotate = bool(rotate)
        self.reflect = bool(reflect)
        self.alt = bool(alt)
        self.tiling = bool(tiling)
        self.name = name

    @property
    def width(self):
        return self.umax - self.umin

    @property
    def height(self):
        return self.vmax - self.vmin

    @property
    def area(self):
        return self.width * self.height

    @property
    def aspect(self):
        """Directional aspect (width/height); >1 wide, <1 tall."""
        return self.width / max(self.height, 1e-9)

    @property
    def posaspect(self):
        """Orientation-independent aspect magnitude, always >= 1."""
        a = self.aspect
        return a if a >= 1.0 else 1.0 / max(a, 1e-9)

    @property
    def short_side(self):
        return min(self.width, self.height)

    def is_full_width(self):
        return self.umin <= FULL_WIDTH_TOL and self.umax >= 1.0 - FULL_WIDTH_TOL

    def __repr__(self):
        return "Rect(%.4f, %.4f, %.4f, %.4f%s)" % (
            self.umin, self.vmin, self.umax, self.vmax,
            "".join(", %s" % f for f in ("rotate", "reflect", "alt", "tiling") if getattr(self, f)))


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

Candidate = namedtuple("Candidate", "index rect swap aspect_score size_score")
StripCandidate = namedtuple("StripCandidate", "index rect rotated score pref")


def _pool(rects, use_alt):
    """Alt rects are only eligible while Alt is held (Strata convention);
    when Alt is held, alts take priority if any exist."""
    pool = list(enumerate(rects))
    if use_alt:
        alts = [(i, r) for i, r in pool if r.alt]
        return alts if alts else pool
    return [(i, r) for i, r in pool if not r.alt]


def rank_rects(patch_aspect, patch_area_world, rects, scale,
               world_orient=True, allow_flip=True, use_alt=False):
    """Rank rects for an island patch: closest quantized aspect first, then
    closest area (Hammer/DreamUV two-stage). `scale` = world units per UV tile.
    Returns Candidates sorted best-first; swap=True means rotate the patch 90°
    into the rect."""
    patch_aspect = max(patch_aspect, 1e-6)
    if world_orient:
        qp = math.log(quantize_aspect(patch_aspect))
    else:
        qp = math.log(quantize_aspect(max(patch_aspect, 1.0 / patch_aspect)))
    p_area = math.log(max(patch_area_world, 1e-12))

    cands = []
    for i, r in _pool(rects, use_alt):
        if world_orient:
            variants = [(math.log(quantize_aspect(r.aspect)), False)]
            if r.rotate:
                variants.append((math.log(quantize_aspect(1.0 / max(r.aspect, 1e-9))), True))
        else:
            mismatch = (r.aspect >= 1.0) != (patch_aspect >= 1.0)
            swap = mismatch and (allow_flip or r.rotate)
            variants = [(math.log(quantize_aspect(r.posaspect)), swap)]
        qa, swap = min(variants, key=lambda t: abs(t[0] - qp))
        size_score = abs(math.log(max(r.area * scale * scale, 1e-12)) - p_area)
        cands.append(Candidate(i, r, swap, abs(qa - qp), size_score))
    cands.sort(key=lambda c: (round(c.aspect_score, 9), round(c.size_score, 9), c.index))
    return cands


def choose(cands, rng, size_margin=0.0):
    """Pick among candidates tied with the best: identical quantized aspect and
    size within `size_margin` (fraction) — random tie-break, re-rollable."""
    best = cands[0]
    smax = best.size_score + math.log(1.0 + max(size_margin, 0.0)) + 1e-9
    ties = [c for c in cands
            if c.aspect_score <= best.aspect_score + 1e-9 and c.size_score <= smax]
    return rng.choice(ties)


def rank_strip_rects(width_world, rects, scale, use_alt=False):
    """Rank rects for a quad strip: match the rect's short (band) dimension to
    the strip's cross-section width; prefer tiling / full-width rects among
    near-equal matches."""
    width_world = max(width_world, 1e-9)
    out = []
    for i, r in _pool(rects, use_alt):
        variants = [(r.height, False)]
        if r.rotate:
            variants.append((r.width, True))
        h_uv, rotated = min(
            variants, key=lambda t: abs(math.log(max(t[0], 1e-9) * scale / width_world)))
        score = abs(math.log(max(h_uv, 1e-9) * scale / width_world))
        pref = 0 if (r.tiling or r.is_full_width()) else 1
        out.append(StripCandidate(i, r, rotated, score, pref))
    out.sort(key=lambda c: (round(c.score, 3), c.pref, c.score, c.index))
    return out


def choose_strip(cands, rng, size_margin=0.0):
    best = cands[0]
    lim = best.score + math.log(1.0 + max(size_margin, 0.0)) + 1e-9
    ties = [c for c in cands if c.score <= lim and c.pref == best.pref]
    return rng.choice(ties)


# ---------------------------------------------------------------------------
# Valve KeyValues .rect I/O
# ---------------------------------------------------------------------------

def _tokenize(text):
    text = re.sub(r"//[^\n]*", "", text)
    toks = re.findall(r'"[^"]*"|\{|\}|[^\s{}]+', text)
    return [t[1:-1] if t.startswith('"') else t for t in toks]


def _parse_block(toks, i):
    """Parse `key value` / `key { ... }` pairs until '}' or EOF.
    Returns (list of (key_lower, str_or_sublist), next_index)."""
    items = []
    while i < len(toks):
        t = toks[i]
        if t == "}":
            return items, i + 1
        key = t
        i += 1
        if i < len(toks) and toks[i] == "{":
            sub, i = _parse_block(toks, i + 1)
            items.append((key.lower(), sub))
        elif i < len(toks):
            items.append((key.lower(), toks[i]))
            i += 1
    return items, i


def parse_rect(text, tex_w, tex_h):
    """Parse Valve .rect KeyValues text into normalized Rects (Y flipped)."""
    tex_w = max(tex_w, 1)
    tex_h = max(tex_h, 1)
    items, _ = _parse_block(_tokenize(text), 0)

    block = None
    for k, v in items:
        if k == "rectangles" and isinstance(v, list):
            block = v
            break
    if block is None:
        block = items  # tolerate files with no root name

    def truthy(s):
        return str(s).strip().lower() not in ("0", "", "false", "no")

    rects = []
    for k, v in block:
        if k != "rectangle" or not isinstance(v, list):
            continue
        d = {kk: vv for kk, vv in v if isinstance(vv, str)}
        try:
            x0, y0 = (float(p) for p in d["min"].replace(",", " ").split()[:2])
            x1, y1 = (float(p) for p in d["max"].replace(",", " ").split()[:2])
        except (KeyError, ValueError, IndexError):
            continue
        r = Rect(x0 / tex_w, 1.0 - y1 / tex_h, x1 / tex_w, 1.0 - y0 / tex_h,
                 rotate=truthy(d.get("rotate", "0")),
                 reflect=truthy(d.get("reflect", "0")),
                 alt=truthy(d.get("alt", "0")))
        r.tiling = r.is_full_width()
        rects.append(r)
    return rects


def export_rect(rects, tex_w, tex_h):
    """Serialize Rects to Valve .rect KeyValues (pixels, top-left origin)."""
    lines = ["Rectangles", "{"]
    for r in rects:
        x0 = int(round(r.umin * tex_w))
        x1 = int(round(r.umax * tex_w))
        y0 = int(round((1.0 - r.vmax) * tex_h))
        y1 = int(round((1.0 - r.vmin) * tex_h))
        lines += ["\trectangle", "\t{"]
        lines.append('\t\tmin\t\t"%d %d"' % (x0, y0))
        lines.append('\t\tmax\t\t"%d %d"' % (x1, y1))
        for flag in ("rotate", "reflect", "alt"):
            if getattr(r, flag):
                lines.append("\t\t%s\t\t1" % flag)
        lines.append("\t}")
    lines.append("}")
    return "\n".join(lines) + "\n"
