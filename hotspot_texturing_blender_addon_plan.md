# SpotWeld — Technical Research & Implementation Plan for a Strip-Aware Hotspot UV Add-on (Blender 5.1/5.2)

## TL;DR
- **Hotspot texturing works by measuring each geometry patch's aspect ratio + area, then choosing the closest-matching axis-aligned rectangle from a reference sheet (with optional rotation/flip)** — DreamUV implements exactly this but is fundamentally *island-oriented*: it auto-splits a selection into UV islands and fits each island's bounding box to a single rectangle, which is why continuous multi-quad STRIPS (beveled edges, trims) fail — the strip becomes one island whose bounding box is stretched across one rectangle instead of tiling the rectangle along the run.
- **The strip problem is solvable** by adding a dedicated "strip mode" that detects the face-loop/edge-ring running direction, unwraps the run with Follow Active Quads (Length Average) to preserve per-quad texel density, and maps the strip's width to a tiling rectangle (repeated/tiled along U), rather than treating the strip as one bounding box. This is the key differentiator over DreamUV.
- **Build it as a Blender Extension** (`blender_manifest.toml`, `blender_version_min = "5.1.0"`) exposing operators registered for both `VIEW_3D` and `IMAGE_EDITOR`, backed by a shared context-free core module, GPU draw handlers for feedback in both editors, and full `.rect` import/export for Hammer++/Strata Hammer/Source 2 compatibility. You must write against Blender 5.0's new synchronized UV selection system natively.
- **New: an optional "predict the atlas" pre-pass.** The add-on can analyze selected mesh(es), cluster their faces/islands/strips into a small set of reused rectangle "buckets," and hand those to Blender's own UV packer to propose a starting `.rect` layout + labeled preview texture — so the user isn't guessing at atlas layout blind before texturing even begins. This is advisory only, never required, and never touches real mesh UVs.

## Key Findings

### 1. How hotspot / trim-sheet matching algorithms work
The canonical algorithm (Valve Source 2 Hammer, Hammer++, DreamUV, Zen UV, Scythe) is:
1. **Measure the target patch** — compute the UV (or world-projected) bounding box of the selected face/island, deriving its **aspect ratio** (width/height) and its **area/size** (a proxy for texel density).
2. **Match against a catalogue of rectangles** — the reference sheet is a list of axis-aligned rectangles (each with min/max coords). The algorithm finds the closest aspect ratio first, then within that aspect "bucket" the closest area, breaking ties randomly for visual variety.
3. **Handle orientation** — if world orientation is enabled, the island is rotated so its "up" matches world up and the raw aspect (wide >1 vs tall <1) is preserved. If orientation is free, tall and wide variants are interchangeable (a wide rect can serve a tall patch by swapping U/V).
4. **Handle rotation/flip** — rectangles flagged `rotate` may be rotated to fit; flagged `reflect` may be randomly mirrored; square patches get random 90° cycles + mirroring to break repetition.
5. **Apply UVs** — the patch's normalized (0–1) UVs are scaled/offset into the target rectangle's min/max, optionally inset by a pixel margin to respect bevels.

Source 2/Hammer++ behaviour, verbatim from the Valve Developer Community "Hotspot texturing" wiki: *"Hammer will try choose the closest matching rectangle within a margin of error. If there are multiple matches for a rectangle, a random one is chosen. Pressing Fit will generate a new random result in this case."* The same page confirms *"Valve used hotspot texturing extensively in the creation of Half-Life: Alyx"* and that the feature is *"only available in Hammer++ and Strata Hammer."* Replicate this closest-match + random-tie-break + re-roll-on-refit behaviour for compatibility.

### 2. How DreamUV implements hotspotting (and why strips break)
DreamUV's `DREAMUV_OT_hotspotter` operator (`view3d.dreamuv_hotspotter`, in `DUV_HotSpot.py`) works entirely in the 3D viewport in edit mode. Its pipeline:
1. **Applies the hotspot material**, then **creates a working duplicate** so the original is untouched until the end.
2. **Bakes hard edges/seams**: applies "Smooth by Angle" (defaults to 30° / 0.523599 rad if no modifier), marks seams on seams+sharp edges, then `edge_split`s them.
3. **Unwraps** the whole selection with `bpy.ops.uv.unwrap(method='CONFORMAL', margin=1.0)`.
4. **Splits into UV islands** by iterating `bpy.ops.mesh.select_linked(delimit={'UV'})`, collecting each island's faces.
5. **For each island**: computes its UV bounding box → runs `DUV_Utils.square_fit()` to test/lay-out it as a rectangle → if not rectangular, falls back to a plain conformal unwrap → rotates to world angle via `get_orientation()` → normalizes UVs to 0–1 → computes aspect ratio and area (`calc_area()`), correcting area by the filled-vs-bounding ratio from `get_uv_ratio()`.
6. **Matches**: quantizes aspect (wide → `round(aspect)`; tall → `1/round(1/aspect)`), finds the closest atlas aspect, buckets all rects of that aspect, finds the closest by size, and picks randomly among equal-size matches. Flips U/V when the matched rect's orientation is reversed vs the patch.
7. **Applies UVs** into the chosen rect's min/max (with optional pixel inset), applies random mirroring/cycling if world-orientation is off, then transfers UVs back to the original mesh and deletes the duplicate.

**Internal data structures (confirmed from `DUV_Utils.py` source):** The atlas is **not** a `.rect` file — it is a *mesh object* (`context.scene.subrect_atlas`) whose faces' UV bounding boxes define the rectangles. `read_atlas()` reads each face into a `subrect` object with four fields:
- `aspect` — directional aspect ratio, quantized: `>1` wide via `round(aspect)`, `<1` tall via `1/round(1/aspect)`.
- `posaspect` — orientation-independent magnitude, always `≥1` (a 2:1 wide rect and a 1:2 tall rect both give `posaspect==2`). Matching uses `aspect` when world-orientation is on, `posaspect` when off (so wide/tall become interchangeable and flippable).
- `size` — the face's real 3D area ÷ `duvhotspotscale²`, rounded to 2 significant figures.
- `uvcoord` — list of copied corner UV `Vector`s.

`square_fit()` returns `not distorted` and **explicitly rejects non-rectangular topology**: it returns `False` for donut/ring shapes, for any boundary interior angle `>230°` (concave corner), for more than four ~90° corners (`NCount>4`), for a 4th-best corner deviating `>125°` from square, or for zero-length edges. Its quad path uses `follow_active_quads()` to flatten the island, then normalizes to a single rectangle. `get_orientation()` finds which UV corners map to the highest world-space Z and applies a 0/90/180/270° UV rotation to align the island to world up. `get_uv_ratio()` temporarily projects the selected verts onto their UV coords (z=0), sums `calc_area()` to get the true filled UV-island area, then restores the coords — this filled area is divided into the bounding-box size so matching reflects occupied area, not bounding box.

**Why strips fail — root cause analysis:**
- DreamUV fits each **island's bounding box** to a **single rectangle**. A continuous strip of N quads forming a beveled edge is one connected UV island; its bounding box gets mapped to one rectangle, so the trim texture is stretched once across the whole strip instead of tiling per segment. There is no concept of "tile this rectangle along the strip's length."
- `square_fit()` rejects bent/complex strips (concave corners, >4 corners, donuts), so trims that turn corners fall back to a plain conformal unwrap, losing hotspot alignment entirely.
- The quad path *can* run `follow_active_quads()` on a straight run, but DreamUV only uses it to collapse the island into one rectangle for bounding-box matching — it never maps to a *tiling* rect, so per-quad texel density along the run is not preserved.
- DreamUV's README concedes the limitation, verbatim: *"The mesh will be split into multiple uv islands that are hotspotted individually, using hard edges and seams. Its highly recommended to place extra seams manually to guide the tool and try to divide up your geometry into rectangular patches."* — i.e., the user must manually pre-cut strips into rectangular chunks.

Known GitHub issues confirm the tool is finicky (e.g. #24 "Strange/flipped faces/normals" on hotspot; #53 on complex atlas usage). There is no built-in strip/tiling mode. DreamUV is GPL, so reusing its logic obligates GPL licensing (fine — Blender add-ons must be GPL anyway).

### 3. Other tools & techniques
- **Valve Source 2 Subrect Editor / Image Subrect Editor** (authoritative UX reference) — draws rectangles on a texture with `LMB+drag`, grid snap via `[`/`]`, per-rect flags: **Allow Rotation**, **Allow Tiling** (tiles *horizontally only*, always full texture width, never vertically), **Inset X/Y** (default bevel inset used by the Fast Texture Tool). The horizontal-only tiling model directly informs strip handling.
- **Hammer++ / Strata Hammer** — reads `.rect` via `%rectanglemap`; "Fit" button matches nearest rectangle; Strata adds flags `rotate`, `reflect`, `alt`.
- **Zen UV (Blender)** — most mature Blender competitor. "Hotspot Mapping" matches islands to trims by **Area, Aspect, World Size, and Tags**. Its **Quick Hotspot** operator, verbatim from Zen UV 5.3 docs, *"can work with existing islands or create them based on the selected polygons... Islands will be transformed into rectangles when possible... The Quick Hotspot operator is available only in the 3D View context, since the UV Editor lacks sharp edge information, making it impossible to generate predictable islands."* It exposes World Orient, Allow Flip, Inset, and Matching Scale, plus a "Fit Axis = Min" mode that lets islands extend beyond trim bounds (i.e., tile) and Area Matching modes (As is / Max / Min / Manual). Confirms tag-based and axis-based matching as best practice.
- **Scythe (UE editor)** — "Apply Selected Patch to Individual Faces" (Shift+F); modes, verbatim: *"Automatic: Tries Square first and falls back to Conforming if it's too distorted / Square / Conforming / Follow Active Quads: Straightens quads/polygroups."* Tiling, verbatim: *"Scythe will treat tiling patches as a trim, meaning it's meant to tile forever... Can only use for patches that take up 100% of either the width or height of the texture."* This confirms both Follow-Active-Quads as the strip-handling primitive and the full-width/height tiling convention.
- **UModeler (Unity)** — `.asset` hotspot layouts, rectangle + triangle regions, padding, Auto Hotspot on edit.
- **Mallet (Blender, Gumroad, by dertwist)** — closest existing competitor: "Interactive hotspot fitting for trim sheets and atlas textures, with **.rect import/export**, automatic best-fit logic, interactive picking, straighten and randomization tools, plus texel-density controls" and "Continue UVs from an active face across selected faces... preserving texel density." Study it for `.rect` round-tripping and interactive picking UX.
- **RectMaker** (cplbradley) and **XBLAH's Modding Tool Material Hotspot Editor** — standalone `.rect` GUI editors.
- **Blender native primitives**: **Follow Active Quads** (`bpy.ops.uv.follow_active_quads`, Edge Length Mode: Even / Length / Length Average) is the built-in tool for laying out a quad strip along its run — the algorithmic backbone for strip mode. **Select Linked** with `delimit={'UV','SEAM','SHARP','MATERIAL'}` isolates islands.

### 4. The `.rect` file format specification
Plaintext Valve KeyValues, placed beside the VMT/VTF in the `materials` folder. Origin (0,0) = **top-left** of texture; coords in **pixels**:
```
Rectangles
{
	rectangle
	{
		min		"0 0"
		max		"512 32"
	}
	rectangle
	{
		min		"512 0"
		max		"768 32"
	}
}
```
- Referenced in the VMT via `%rectanglemap "path/to/name"` (no extension; assumed under `materials`).
- **Region flags** (added as `flagname 1` inside a rectangle block). Strata Hammer supports:
  - `rotate 1` — region may be rotated to better match the surface.
  - `reflect 1` — region may be randomly horizontally flipped.
  - `alt 1` — marks region as an alternate; only chosen when the Alt key is held.
- Source 2's Subrect Editor additionally exposes **Allow Tiling** (horizontal only, full texture width) and **Inset X/Y** — express tiling as a rectangle spanning full width, with the consuming tool tiling it along U.
- The add-on must round-trip: import (pixel coords → normalized 0–1 dividing by texture dimensions, flipping Y because Blender UV origin is bottom-left vs Valve top-left) and export back to this exact format.

### 5. Blender 5.1/5.2 add-on best practices
- **Packaging as an Extension**: Per the Blender 4.2 LTS Python API release notes, verbatim: *"With the new extensions platform, there is also a new method for packaging add-ons. It uses a separate blender_manifest.toml file instead of bl_info embedded in the script. The old packaging still works but is considered legacy, and add-ons are recommended to switch to the new manifest file."* Required manifest keys (confirmed in the Blender 5.1 Manual): `schema_version = "1.0.0"`, `id`, `version`, `name`, `tagline`, `maintainer`, `type = "add-on"`, `blender_version_min` (set `"5.1.0"`), `license` (SPDX list, e.g. `["SPDX:GPL-3.0-or-later"]`). Optional: `tags = ["UV","Mesh","3D View"]`, `blender_version_max`, `platforms`, `wheels`, and `[permissions]` with `files = "Import/export .rect files"` (declare it — the add-on reads/writes files). The zip must contain a folder named after `id` with the manifest + `__init__.py`. Build with `blender --command extension build`. A legacy `bl_info` can coexist for older-Blender fallback but is unnecessary if targeting only 5.1/5.2.

  Concrete manifest for this add-on:
  ```toml
  schema_version = "1.0.0"
  id = "spotweld"
  version = "0.1.0"
  name = "SpotWeld"
  tagline = "Strip-aware hotspot UV texturing for trims, panels, and .rect atlases"
  maintainer = "Your Name <you@example.com>"
  type = "add-on"
  blender_version_min = "5.1.0"
  license = ["SPDX:GPL-3.0-or-later"]
  tags = ["UV", "Mesh", "3D View"]

  [permissions]
  files = "Import/export .rect hotspot atlas files"
  ```
- **API patterns**: use `bmesh.from_edit_mesh(obj.data)` in edit mode; `uv_layer = bm.loops.layers.uv.verify()`; iterate `for loop in face.loops: loop[uv_layer].uv`; call `bmesh.update_edit_mesh(obj.data)` to commit. **Do not use `bgl`** — per Blender Developer docs it was deprecated in 3.5 (with the runtime warning "In Blender 4.0 'bgl' will be removed") and is gone; use the `gpu` module + `gpu_extras.batch.batch_for_shader`. Also avoid `material.texture_slots` (2.7x-era dead API — DreamUV still contains such dead code in `get_face_pixel_step`).
- **CRITICAL — Blender 5.0 UV sync selection change**: Blender 5.0 introduced synchronized UV selection **enabled by default**, storing UV selection per face-corner and allowing individual UV coords to be selected without selecting all UVs on a vertex. This changed or broke many UV add-ons (UniV's changelog documents "a tremendous amount of work to support Blender 5.0"; operators no longer auto-switch to Face mode on deselect). DreamUV's hotspotter explicitly toggles `use_uv_select_sync` off during processing and restores it after — the new add-on should instead be written against the 5.0+ sync model natively (check `context.scene.tool_settings.use_uv_select_sync`; use the new per-corner selection API). Blender 5.1 also split "Loop Multi Select" into a separate "Select Edge Ring" (per UniV's 5.1 note) — relevant to strip selection.
- **Operators in BOTH editors**: branch on `context.space_data.type` (`'VIEW_3D'` vs `'IMAGE_EDITOR'`); write a `poll()` accepting both. Provide two N-panel classes (`bl_space_type='VIEW_3D'` / `'IMAGE_EDITOR'`, `bl_region_type='UI'`). Put all real work in a context-free core module (bmesh + uv_layer in, UVs out) so both entry points call identical code. Use `context.temp_override(...)` (3.2+) to invoke editor-specific ops from the wrong context.
- **WorkSpaceTool**: subclass `bpy.types.WorkSpaceTool` with `bl_space_type` (`'VIEW_3D'` or `'IMAGE_EDITOR'`), `bl_context_mode='EDIT_MESH'`, `bl_idname`, `bl_widget` (gizmo group), `bl_keymap`. Register one per space to appear in both editors.
- **Gizmos**: `GizmoGroup` with `bl_space_type`/`bl_region_type` per editor; `bl_options` may include `'SHOW_MODAL_ALL'`, `'DEPTH_3D'`, `'SELECT'`.
- **Draw handlers**: `bpy.types.SpaceView3D.draw_handler_add(cb, (), 'WINDOW', 'POST_VIEW')` for 3D and `bpy.types.SpaceImageEditor.draw_handler_add(cb, (), 'WINDOW', 'POST_PIXEL')` for the UV editor. Build batches with `batch_for_shader(gpu.shader.from_builtin('UNIFORM_COLOR'|'POLYLINE_UNIFORM_COLOR'), 'LINES'|'TRIS', {...})`; `area.tag_redraw()` to refresh; always remove handlers on unregister and modal exit.

### 6. Custom geometry selection & modal UX
- **Strip / face-loop selection**: the classic edge-loop walk is `loop = loop.link_loop_prev.link_loop_radial_prev.link_loop_prev` (fails on mixed normals — normalize or track direction). For strips, prefer **edge rings / face loops**: from a seed quad, walk `link_loop_radial_next` across shared edges, using `Mesh Walk Delimit Face Loop Items`. Native `bpy.ops.mesh.loop_multi_select(ring=True)` and (5.1+) "Select Edge Ring" can seed the selection; then read `bm.select_history` for the active/seed face and order the strip by connectivity. Non-quad faces terminate a strip. Handle the self-intersecting/cyclic face-loop edge cases from Blender's tracker (T30504) — stop cleanly at branches and non-quads.
- **Strip direction**: for each quad, the two edges shared with neighbours define the "length" (tiling) axis; the perpendicular pair defines the "width" axis mapped to the rectangle's short dimension. Follow Active Quads with "Length Average" then preserves per-quad proportion along the run.
- **`bm.select_history`**: `bm.select_history.active` (or `[-1]`) gives the active element — use it as the strip seed and Follow-Active-Quads anchor. Add/remove with `.add()`/`.remove()`; `.validate()` to ensure.
- **Modal operator UX**: `invoke()` → `context.window_manager.modal_handler_add(self)` → `{'RUNNING_MODAL'}`; in `modal()` handle `MOUSEMOVE` (hover highlight via draw handler + select under cursor), `LEFTMOUSE` (commit/cycle rect variation), `WHEEL`/keys (cycle candidate rects, toggle rotate/flip), `RIGHTMOUSE`/`ESC` (cancel/restore). Return `{'PASS_THROUGH'}` for navigation so the user can orbit while active. Back up UVs on invoke for clean cancel (mirror DreamUV's non-destructive duplicate pattern). Click-to-cycle variations (à la DreamUV/Hammer "Fit") is the expected idiom.

### 7. Predictive atlas suggestion — no direct prior art, but the underlying primitives already exist
A dedicated search for a tool that analyzes a mesh and proposes what an atlas/trim-sheet layout should contain turned up plenty of tools that help you *pack* a texture once you already know what regions you need (Blender's native packer, UVPackmaster, Zen UV), and even an AI tool — TrimSheetFast — that generates trim-sheet *textures* from a hand-built template, but nothing that derives the template from geometry. TrimSheetFast's own FAQ makes the direction explicit, stating you only need to describe your materials and define your regions, and confirming it works without needing your mesh at all. Community trim-sheet tutorials likewise treat layout planning as a manual step done "in a 2D format" as "a quick sketch" before any modeling happens. This confirms mesh-driven atlas prediction is a genuine gap rather than a reimplementation, but it also means there's no reference implementation to benchmark quality against — budget extra design iteration here.

The good news is the two hard sub-problems are both already solved by primitives this plan already needs elsewhere: **shape analysis** is the same island/strip detection built for Phase 3/4, and **layout** doesn't need a bespoke bin-packer — Blender's own UV packer (exposed both as the `bpy.ops.uv.pack_islands` operator and, per the Blender 5.1 Manual, as a Geometry Nodes "Pack UV Islands" node) already supports a `rotate` option and a `Shape Method` that "uses the axis-aligned bounding box of each island... the fastest method" up through a slower, tighter "full island shape, including concave regions and holes" mode — exactly the tightness/speed tradeoff an atlas predictor needs, without writing and maintaining a MaxRects/Skyline/guillotine packer from scratch. Third-party packer UVPackmaster additionally demonstrates the other missing piece — scaling islands to a specific texel density *before* packing while keeping that scale fixed *during* packing — which is the mechanism that keeps predicted rectangles texel-density-consistent by construction.

## Details: Recommended architecture

**Module layout** (folder `spotweld` matching manifest `id`):
- `blender_manifest.toml`
- `__init__.py` — registration only (classes, draw handlers, tools).
- `core_match.py` — pure logic: `RectList` (`.rect` import/export), aspect/area/tag matching `best_fit(patch, rects, orient, allow_rotate, allow_flip, tiling)`, no bpy dependency. Unit-testable headless.
- `core_geometry.py` — bmesh helpers: island detection, **strip detection & ordering**, Follow-Active-Quads-based strip layout, world-orientation rotation, texel-density calc.
- `core_atlas_suggest.py` — **new**: patch enumeration (calls into `core_geometry.py`'s island/strip detection), similarity clustering into reusable buckets, bucket→rectangle sizing from a target texel density. No bpy dependency beyond reading mesh data; unit-testable like `core_match.py`.
- `ops_fit.py` — fit operators (single-face/island + **strip mode**), non-destructive with UV backup.
- `ops_select.py` — strip/loop selection helpers + the modal interactive tool.
- `ops_rect_io.py` — `.rect` import/export operators.
- `ops_suggest_atlas.py` — **new**: the `mesh.spotweld_suggest_atlas` operator — builds placeholder geometry from `core_atlas_suggest.py`'s buckets, invokes `bpy.ops.uv.pack_islands`, reads back results, writes `.rect` via `ops_rect_io.py`, renders a labeled preview PNG, cleans up placeholders. Touches only temporary scratch geometry — never the original mesh's UVs.
- `ui.py` — N-panels for `VIEW_3D` and `IMAGE_EDITOR`; property groups. **New:** an "Atlas Prediction (optional)" collapsible section exposing texture resolution, target texel density, an "Atlas Economy" preset selector (**Lean / Balanced / High Fidelity**, each mapping to a preset aspect+size tolerance — see algorithm below — with an "Advanced" disclosure exposing the raw numeric tolerance for power users), a strip-vs-island handling toggle, and the "Suggest Atlas" button — visually separated from the main Fit workflow so it reads as optional.
- `draw.py` — GPU draw handlers for both spaces (highlight target patch, preview chosen rect, draw rect grid over the texture in the UV editor).
- `tools.py` — WorkSpaceTool subclasses for both spaces.

**Strip mode algorithm (the core improvement over DreamUV):**
1. User selects a run of connected quads (or seeds one quad and invokes "grow strip" to walk the face loop).
2. Order quads along the strip via loop connectivity; detect length vs width axis from shared edges.
3. Unwrap with Follow Active Quads (Length Average) anchored on the active quad, giving each quad UV length proportional to its 3D edge length — preserving texel density along the run.
4. Choose a rectangle whose **short dimension** (height) matches the strip width by aspect+area, preferring rects flagged tiling/`rotate`.
5. Map strip width → rect height; **tile the rect along U** to cover the strip's total length (repeating the rect, or a full-width tiling rect à la Source 2's horizontal-only tiling), keeping texel density consistent. Apply pixel inset for bevels.
6. Support the strip bending around corners (unlike `square_fit`, which rejects bends) because Follow Active Quads handles bends per-quad.

**`.rect` compatibility rules:**
- Import: `u = px_x / tex_width`, `v = 1 - (px_y / tex_height)` (flip Y). Store both normalized and original pixel coords.
- Export: reverse; write min = top-left, max = bottom-right in pixels; emit `rotate/reflect/alt` flags from per-rect toggles; document the Allow-Tiling (full-width) convention.
- Optionally read/write the DreamUV atlas-mesh format for interop.

**Predictive atlas suggestion algorithm (new, optional pre-pass):**

Goal: given a selection of mesh(es), produce a suggested `.rect` layout plus a labeled placeholder preview texture — a starting point for a texture artist — without ever touching real UVs or being a required step.

1. **Patch extraction** — run the Phase 3/4 island- and strip-detection logic against the current selection to enumerate every face/island/strip that will eventually need a rectangle. Strip detection is a hard prerequisite for a *useful* predictor: without it, every trim strip would demand its own oversized rectangle, recreating DreamUV's stretching problem one step earlier in the pipeline.
2. **Per-patch sizing key** — for an island/face patch: aspect ratio + real-world area (the same fields DreamUV already computes). For a strip patch: only the *cross-strip width* (short axis) matters, normalized by target texel density — length is irrelevant because the eventual rectangle tiles along U. This is what keeps the suggested atlas small: a 40-quad curved railing and a 6-quad straight pipe seam can share one tiling rectangle if their cross-sections match, despite wildly different lengths.
3. **Similarity clustering (the "reuse" lever)** — sort patches by size descending; walk the list, greedy-merging each unmerged patch into the first existing bucket whose aspect and size both fall within a tolerance, otherwise starting a new bucket. Rather than exposing a raw numeric tolerance as the primary control — there's no existing tool to borrow sensible default values from, so a bare slider would just be a guessing game for the user — default the UI to three named "Atlas Economy" presets: **Lean** (~30% aspect/size tolerance — favors reuse, smallest rectangle count, most visible repetition), **Balanced** (~15% tolerance — a reasonable middle ground), and **High Fidelity** (~5% tolerance — close to one rectangle per unique patch, maximum texture space, minimal reuse). An "Advanced" disclosure underneath the three presets exposes the raw numeric tolerance directly for users who want to fine-tune beyond them. This mirrors DreamUV's own aspect/size quantization for matching — the new step performs that quantization *before* an atlas exists, to decide what it should contain.
4. **Rectangle sizing per bucket** — derive each bucket's representative rectangle width/height from its normalized aspect and a global texel-density target (user input, e.g. "512 px/m"), so every suggested rectangle is texel-density-consistent by construction.
5. **Layout via Blender's own packer** — generate one placeholder n-gon per bucket sized to its target rectangle, and pack them with `bpy.ops.uv.pack_islands(rotate=True, shape_method='AABB', scale=False)` (fixed-scale, so pre-sized rectangles keep their texel-density-correct dimensions), rather than writing a bespoke bin-packer.
6. **Output, not mutation** — read back packed placeholder positions, convert to pixel rectangles at the chosen texture resolution, and emit (a) a `.rect` file in the Phase 2 format and (b) a flat-color labeled preview PNG (via the same offscreen `gpu` pipeline used for Phase 5's draw handlers). No mesh UV data is ever written.
7. **Non-destructive & re-runnable** — a single operator, re-runnable any time selection or geometry changes, since it never touches real UVs. Once an artist paints a real texture over the suggested layout, its `.rect` file becomes an ordinary atlas input to the normal Phase 3/4 fitting operators — there's no special "predicted atlas" data model to maintain long-term.

## Recommendations

**Staged build plan for Claude Code:**
1. **Phase 1 — Skeleton & packaging.** Folder, `blender_manifest.toml` (`blender_version_min="5.1.0"`, `[permissions] files=...`), `__init__.py`. *Benchmark: enables/disables without console errors in both 5.1 and 5.2 via Install from Disk.*
2. **Phase 2 — `.rect` I/O + core matching (headless-testable).** `core_match.py` import/export + `best_fit`. *Benchmark: exported file matches Valve's KeyValues layout; import→export is idempotent against a real Hammer++ `.rect`.*
3. **Phase 3 — Single-face/island fit** in both editors, non-destructive, DreamUV parity (aspect+area+random tie-break, world orient, flip). *Benchmark: on a beveled cube, results match DreamUV.*
4. **Phase 4 — STRIP MODE (the differentiator):** strip detection/ordering + Follow-Active-Quads layout + tiling-rect mapping. *Benchmark: a 10-quad curved trim strip textures with consistent texel density and no stretching — the case DreamUV fails.*
5. **Phase 4.5 — Predictive Atlas Suggestion (optional, depends on Phase 2 + Phase 4).** Patch extraction reusing island/strip detection; greedy similarity clustering; placeholder generation + native `pack_islands` call; `.rect` + labeled preview PNG output. *Benchmark: on a modular wall kit with ~30 mixed faces/strips, the tool proposes a materially smaller rectangle count than "one per patch," the three Atlas Economy presets produce rectangle counts in the expected order (Lean < Balanced < High Fidelity), the Advanced numeric override reproduces intermediate results between presets, the exported `.rect` round-trips through Phase 2's importer, and the operator makes zero changes to the mesh's actual UV layer.*
6. **Phase 5 — Interactive modal tool + gizmos + draw handlers** in both spaces; click-to-cycle variations; hover highlight; rectangle-grid overlay on the texture. *Benchmark: user selects a strip, invokes, cycles rects with the wheel, cancels cleanly with ESC restoring UVs.*
7. **Phase 6 — Polish:** tag support, texel-density controls, multi-object, presets, UV-editor rect painting (Subrect-Editor equivalent), DreamUV atlas import.

**Design thresholds that change the plan:**
- If users are on Blender ≤4.4, add a legacy `bl_info` + non-sync-selection path; otherwise target 5.1+ sync-native only (recommended — simpler).
- If Source-engine compatibility is the priority, make `.rect` the *primary* data model (not a DreamUV-style atlas mesh). If Blender-only, an atlas mesh or internal `PropertyGroup` rect list is more ergonomic.
- If strips bending around corners are common, per-quad Follow Active Quads is essential; if strips are always straight, a simpler linear parametrization suffices.
- If the target user base mostly builds small/simple kits, defaulting the preset selector to **High Fidelity** is fine; if they build large modular environments — the trim-sheet use case this whole tool targets — **Balanced** is the recommended default, with **Lean** available for users who explicitly want maximum reuse, so the predictor's main value (telling you how few rectangles you can get away with) actually shows up without surprising a first-time user with an overly aggressive default.

## Caveats
- DreamUV internals above were transcribed from the GitHub blob rendering of `DUV_HotSpot.py` and `DUV_Utils.py` (master branch; indentation reconstructed from flattened HTML). Re-verify line-level behaviour against the source before cloning logic. DreamUV is GPL — reuse obligates GPL (fine for a Blender add-on).
- Source 2 "Allow Tiling" semantics (horizontal-only, full-width) are documented on the Valve wiki, but the precise Hammer++ matching tolerance ("margin of error") is not published as a number — expose a user-tunable tolerance.
- Blender 5.0/5.1 UV-sync-selection is still evolving (active tracker tasks #136817, #131642); test against the specific 5.1 and 5.2 releases, as selection-API behaviour has shifted between 5.0, 5.1, and 5.2.
- Some competitor internals (Zen UV, Mallet) are closed-source; their behaviour is inferred from docs/marketing, not code inspection.
- Rectangle/bin-packing literature is largely irrelevant to the core hotspot-fitting problem: hotspotting is a *nearest-match selection* problem (choose an existing rect for a patch), not a *packing* problem (arrange rects to fill space) — packing only becomes relevant for the new Predictive Atlas Suggestion feature, where Blender's native packer is reused rather than a bespoke algorithm.
- No third-party precedent was found for mesh-driven atlas layout prediction specifically; TrimSheetFast is the closest match in spirit but works in the opposite direction (hand-built template + text prompt → generated textures, explicitly without ingesting a mesh). Treat the Predictive Atlas Suggestion feature as novel rather than "catching up" to an existing tool, and budget extra design iteration for the clustering-tolerance UX, since there's no reference implementation to benchmark against.
- The greedy similarity-clustering order (sorted by size descending) is a reasonable default but not necessarily optimal — a smarter global clustering (e.g., k-means on normalized aspect/size) could produce a better bucket count for the same fidelity tolerance. Start greedy for simplicity; revisit only if early users report the suggested bucket counts feel wasteful.
