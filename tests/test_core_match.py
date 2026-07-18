# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless tests for core_match: .rect I/O round-tripping and matching.
Run with plain Python: python tests/test_core_match.py"""

import os
import random
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "spotweld"))

import core_match as cm  # noqa: E402

SAMPLE = """
// comment line
Rectangles
{
\trectangle
\t{
\t\tmin\t\t"0 0"
\t\tmax\t\t"1024 64"
\t}
\trectangle
\t{
\t\tmin\t\t"0 64"
\t\tmax\t\t"256 192"
\t\trotate\t\t1
\t}
\trectangle
\t{
\t\tmin\t\t"256 64"
\t\tmax\t\t"512 192"
\t\treflect\t\t1
\t\talt\t\t1
\t}
}
"""

TEX_W, TEX_H = 1024, 1024


def test_quantize():
    assert cm.quantize_aspect(2.3) == 2.0
    assert cm.quantize_aspect(1.0) == 1.0
    assert abs(cm.quantize_aspect(0.4) - 0.5) < 1e-12
    assert cm.quantize_aspect(7.9) == 8.0
    assert cm.quantize_aspect(0.0) == 1.0


def test_parse():
    rects = cm.parse_rect(SAMPLE, TEX_W, TEX_H)
    assert len(rects) == 3, rects
    r0, r1, r2 = rects
    # first: full width trim at the TOP of the texture (Valve y=0 is top)
    assert abs(r0.umin - 0.0) < 1e-9 and abs(r0.umax - 1.0) < 1e-9
    assert abs(r0.vmax - 1.0) < 1e-9 and abs(r0.vmin - (1.0 - 64.0 / TEX_H)) < 1e-9
    assert r0.tiling and r0.is_full_width()
    assert not r0.rotate and not r0.reflect and not r0.alt
    # second: flags
    assert r1.rotate and not r1.reflect and not r1.alt and not r1.tiling
    assert abs(r1.umax - 0.25) < 1e-9
    # third: reflect + alt
    assert r2.reflect and r2.alt and not r2.rotate


def test_roundtrip_idempotent():
    rects = cm.parse_rect(SAMPLE, TEX_W, TEX_H)
    out1 = cm.export_rect(rects, TEX_W, TEX_H)
    rects2 = cm.parse_rect(out1, TEX_W, TEX_H)
    out2 = cm.export_rect(rects2, TEX_W, TEX_H)
    assert out1 == out2, "export->import->export not idempotent:\n%s\n---\n%s" % (out1, out2)
    assert '"0 0"' in out1 and '"1024 64"' in out1
    assert "rotate\t\t1" in out1 and "reflect\t\t1" in out1 and "alt\t\t1" in out1


def _atlas():
    return [
        cm.Rect(0.0, 1.0 - 64 / 1024, 1.0, 1.0, tiling=True),      # 0: 1024x64 trim
        cm.Rect(0.0, 0.0, 0.25, 0.25),                              # 1: 256x256 square
        cm.Rect(0.25, 0.0, 0.75, 0.25),                             # 2: 512x256 wide 2:1
        cm.Rect(0.75, 0.0, 1.0, 0.5, rotate=True),                  # 3: 256x512 tall 1:2
        cm.Rect(0.0, 0.25, 0.25, 0.5, alt=True),                    # 4: alt square
    ]


def test_rank_islands():
    rects = _atlas()
    rng = random.Random(1)
    # 2:1 wide patch, world size ~ rect 2's world size at scale=1
    cands = cm.rank_rects(2.0, 0.5 * 0.25, rects, 1.0, world_orient=True)
    assert cands[0].index == 2 and not cands[0].swap
    # tall 1:2 patch with world orient: tall rect 3 wins directly
    cands = cm.rank_rects(0.5, 0.25 * 0.5, rects, 1.0, world_orient=True)
    assert cands[0].index == 3 and not cands[0].swap
    # tall 1:2 patch, orientation-free: rect 2 and rect 3 are equal aspect;
    # matching wide rect 2 requires swap
    cands = cm.rank_rects(0.5, 0.125, rects, 1.0, world_orient=False)
    top2 = {c.index for c in cands[:2]}
    assert top2 == {2, 3}, cands
    swap_of = {c.index: c.swap for c in cands}
    assert swap_of[2] is True and swap_of[3] is False
    # square patch: alt rect never picked without use_alt
    cands = cm.rank_rects(1.0, 0.0625, rects, 1.0)
    assert all(not c.rect.alt for c in cands)
    picked = cm.choose(cands, rng, 0.0)
    assert picked.index == 1
    # with use_alt, alts take priority
    cands = cm.rank_rects(1.0, 0.0625, rects, 1.0, use_alt=True)
    assert all(c.rect.alt for c in cands)


def test_rank_no_flip_orientation():
    # Orientation-free but flip disallowed: a mismatched, non-rotatable rect
    # must score worse than a matched-orientation rect instead of winning
    # via the orientation-agnostic posaspect and being applied unrotated.
    rects = [cm.Rect(0.0, 0.0, 0.25, 0.5),    # 0: tall 1:2, no rotate flag
             cm.Rect(0.25, 0.0, 0.75, 0.25)]  # 1: wide 2:1
    cands = cm.rank_rects(2.0, 0.125, rects, 1.0,
                          world_orient=False, allow_flip=False)
    assert cands[0].index == 1 and not cands[0].swap, cands
    assert cands[1].aspect_score > cands[0].aspect_score, cands
    # with flip allowed the tall rect ties again, via swap
    cands = cm.rank_rects(2.0, 0.125, rects, 1.0,
                          world_orient=False, allow_flip=True)
    assert {c.index for c in cands[:2]} == {0, 1}
    swap_of = {c.index: c.swap for c in cands}
    assert swap_of[0] is True and swap_of[1] is False


def test_rank_tex_aspect():
    # 2048x1024 texture (tex_aspect = 0.5): a half-width full-height rect is
    # physically square and must beat a UV-square (physically wide) rect for
    # a square patch.
    rects = [cm.Rect(0.0, 0.0, 0.5, 1.0),   # 0: 1024x1024 px -> square
             cm.Rect(0.5, 0.0, 1.0, 0.5)]   # 1: 1024x512 px -> wide 2:1
    cands = cm.rank_rects(1.0, 1.0, rects, 1.0, tex_aspect=0.5)
    assert cands[0].index == 0, cands
    # strip banding: on the same texture a rect of UV height 0.125 spans
    # 128 px; a strip 128 px wide at scale=1 (2048 px/tile) should match it
    # over a rect of UV height 0.0625 (64 px)
    rects = [cm.Rect(0.0, 0.0, 1.0, 0.125),
             cm.Rect(0.0, 0.5, 1.0, 0.5625)]
    cands = cm.rank_strip_rects(128.0 / 2048.0, rects, 1.0, tex_aspect=0.5)
    assert cands[0].index == 0, cands


def test_pick_rect_at():
    # rect 2 is a bare namespace with reversed corners: PropertyGroup rects
    # don't normalize min/max, so pick_rect_at must do it itself
    reversed_rect = types.SimpleNamespace(umin=0.9, vmin=0.6, umax=0.6, vmax=0.9)
    rects = [cm.Rect(0.0, 0.0, 1.0, 1.0),        # 0: full atlas
             cm.Rect(0.25, 0.25, 0.5, 0.5),      # 1: nested inside 0
             reversed_rect]
    # nested rect wins over the enclosing one (smallest containing)
    assert cm.pick_rect_at(rects, 0.3, 0.3) == 1
    assert cm.pick_rect_at(rects, 0.1, 0.1) == 0
    assert cm.pick_rect_at(rects, 0.7, 0.7) == 2
    # outside everything
    assert cm.pick_rect_at(rects, 1.5, 0.5) == -1
    assert cm.pick_rect_at([], 0.5, 0.5) == -1


def test_choose_tie_reroll():
    rects = [cm.Rect(0.0, 0.0, 0.25, 0.25), cm.Rect(0.25, 0.0, 0.5, 0.25)]
    cands = cm.rank_rects(1.0, 0.0625, rects, 1.0)
    seen = {cm.choose(cands, random.Random(s), 0.0).index for s in range(20)}
    assert seen == {0, 1}, "equal rects should tie-break randomly: %s" % seen


def test_rank_strips():
    rects = _atlas()
    # strip cross-width 64px at scale=1 world/tile -> 64/1024 world units
    cands = cm.rank_strip_rects(64.0 / 1024.0, rects, 1.0)
    assert cands[0].index == 0, cands  # the full-width trim
    assert cands[0].pref == 0 and not cands[0].rotated
    # very wide strip: rotate-flagged tall rect may rotate its width into the band
    cands = cm.rank_strip_rects(0.25, rects, 1.0)
    best = cands[0]
    assert best.rect.height == 0.25 or (best.rect.rotate and best.rotated)


def test_full_width_detection():
    r = cm.Rect(0.0, 0.9, 1.0, 1.0)
    assert r.is_full_width()
    r = cm.Rect(0.1, 0.9, 1.0, 1.0)
    assert not r.is_full_width()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS %s" % name)
            except AssertionError as ex:
                failures += 1
                print("FAIL %s: %s" % (name, ex))
    sys.exit(1 if failures else 0)
