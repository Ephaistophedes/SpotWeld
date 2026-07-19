# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless tests for core_atlas_suggest: clustering monotonicity and shelf
packing invariants. Run with plain Python: python tests/test_atlas_suggest.py"""

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "spotweld"))

import core_atlas_suggest as cas  # noqa: E402


def _mixed_patches(seed=7, n=30):
    rng = random.Random(seed)
    patches = []
    base_widths = [0.05, 0.1, 0.2]
    for i in range(n // 2):
        w = rng.choice(base_widths) * rng.uniform(0.92, 1.08)
        patches.append(cas.Patch('STRIP', rng.uniform(2, 40), w, length=rng.uniform(2, 40)))
    base_dims = [(1.0, 1.0), (2.0, 1.0), (0.5, 0.25)]
    for i in range(n - n // 2):
        l, s = rng.choice(base_dims)
        j = rng.uniform(0.92, 1.08)
        patches.append(cas.Patch('ISLAND', l * j, s * j, area=l * s * j * j))
    return patches


def test_cluster_reduces_count():
    patches = _mixed_patches()
    buckets = cas.cluster_patches(patches, cas.PRESET_TOLERANCE['BALANCED'])
    assert len(buckets) < len(patches), \
        "clustering should propose fewer rects than one-per-patch"
    assert sum(len(b.members) for b in buckets) == len(patches)


def test_preset_ordering():
    patches = _mixed_patches()
    counts = {k: len(cas.cluster_patches(patches, tol))
              for k, tol in cas.PRESET_TOLERANCE.items()}
    assert counts['LEAN'] <= counts['BALANCED'] <= counts['HIFI'], counts


def test_strips_bucket_by_width_only():
    # same cross width, wildly different lengths -> one bucket
    patches = [cas.Patch('STRIP', 40.0, 0.1, length=40.0),
               cas.Patch('STRIP', 2.0, 0.1, length=2.0)]
    buckets = cas.cluster_patches(patches, 0.05)
    assert len(buckets) == 1 and len(buckets[0].members) == 2
    # strips never merge with islands
    patches.append(cas.Patch('ISLAND', 0.1, 0.1))
    buckets = cas.cluster_patches(patches, 0.30)
    kinds = sorted(b.kind for b in buckets)
    assert kinds == ['ISLAND', 'STRIP'], kinds


def test_size_and_pack():
    patches = _mixed_patches()
    buckets = cas.cluster_patches(patches, 0.15)
    tex_w, tex_h, pad = 1024, 1024, 4
    sized, _clamped = cas.size_buckets(buckets, 256.0, tex_w, tex_h)
    placements, used_h = cas.shelf_pack(sized, tex_w, tex_h, pad)
    assert len(placements) == len(buckets)
    boxes = []
    for b, w, h, full, x, y in placements:
        assert x >= 0 and y >= 0
        assert x + w <= tex_w, "island overflows texture width"
        if full:
            assert x == 0 and w == tex_w, "strips must span full width"
        boxes.append((x, y, x + w, y + h))
    _assert_no_overlap(boxes)
    assert used_h == max(y1 for _x0, _y0, _x1, y1 in boxes)


def _is_pow2(n):
    return n > 0 and (n & (n - 1)) == 0


def _assert_no_overlap(boxes):
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            overlap = not (a[2] <= b[0] or b[2] <= a[0]
                           or a[3] <= b[1] or b[3] <= a[1])
            assert not overlap, "rects %d and %d overlap: %s %s" % (i, j, a, b)


def test_pack_full_atlas_covers_exactly():
    patches = _mixed_patches()
    buckets = cas.cluster_patches(patches, 0.15)
    tex_w = tex_h = 512
    placements, used_h, s = cas.pack_full_atlas(buckets, tex_w, tex_h)
    assert used_h <= tex_h and s > 0
    # one placed rect per measured bucket, the rest are fillers
    assert sum(1 for p in placements if p[0] is not None) == len(buckets)
    area = 0
    boxes = []
    for b, w, h, full, x, y in placements:
        assert 0 <= x and 0 <= y and x + w <= tex_w and y + h <= tex_h
        if full:
            assert x == 0 and w == tex_w
        # po2 sizes everywhere on a po2 texture (fillers included)
        assert _is_pow2(w) and _is_pow2(h), (w, h)
        area += w * h
        boxes.append((x, y, x + w, y + h))
    assert area == tex_w * tex_h, "coverage is not exact: %d" % area
    _assert_no_overlap(boxes)


def test_pack_full_atlas_single_bucket_fills():
    buckets = cas.cluster_patches(
        [cas.Patch('ISLAND', 1.0, 1.0, area=1.0)], 0.15)
    placements, used_h, _s = cas.pack_full_atlas(buckets, 256, 256)
    assert len(placements) == 1
    b, w, h, _full, x, y = placements[0]
    assert b is not None and (w, h, x, y) == (256, 256, 0, 0)


def test_pack_full_atlas_strips_only():
    patches = [cas.Patch('STRIP', 10.0, 0.2, length=10.0),
               cas.Patch('STRIP', 10.0, 0.05, length=10.0)]
    buckets = cas.cluster_patches(patches, 0.05)
    placements, _used_h, _s = cas.pack_full_atlas(buckets, 512, 512)
    assert all(full for _b, _w, _h, full, _x, _y in placements)
    assert sum(h for _b, _w, h, _f, _x, _y in placements) == 512
    # the two band buckets keep a po2 ratio near the measured 4:1 — the
    # fill-maximizing scale can land the smaller band exactly on a po2
    # rounding midpoint, where rounding up (ratio 2) is equally valid
    bands = [h for b, _w, h, _f, _x, _y in placements if b is not None]
    assert len(bands) == 2 and all(_is_pow2(h) for h in bands), bands
    assert max(bands) // min(bands) in (2, 4), bands


def test_pack_full_atlas_overflow():
    buckets = [cas.Bucket('ISLAND', 1.0 + 0.5 * i, 1.0 + 0.5 * i, [None])
               for i in range(100)]
    placements, used_h, _s = cas.pack_full_atlas(buckets, 64, 64)
    assert used_h > 64
    # overflow keeps every bucket placed but adds no fillers
    assert len(placements) == 100
    assert all(p[0] is not None for p in placements)


def test_custom_tolerance_between_presets():
    patches = _mixed_patches()
    lean = len(cas.cluster_patches(patches, 0.30))
    mid = len(cas.cluster_patches(patches, 0.22))
    bal = len(cas.cluster_patches(patches, 0.15))
    assert lean <= mid <= bal, (lean, mid, bal)


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
