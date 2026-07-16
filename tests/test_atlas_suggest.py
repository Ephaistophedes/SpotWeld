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
    # no pairwise overlap
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b2 = boxes[i], boxes[j]
            overlap = not (a[2] <= b2[0] or b2[2] <= a[0]
                           or a[3] <= b2[1] or b2[3] <= a[1])
            assert not overlap, "rects %d and %d overlap: %s %s" % (i, j, a, b2)
    assert used_h == max(y1 for _x0, _y0, _x1, y1 in boxes)


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
