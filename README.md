# SpotWeld

Strip-aware hotspot UV texturing for Blender 5.1+ — fit selected geometry to
the closest rectangles of a hotspot/trim-sheet atlas (Hammer++ / Source 2
style), with a dedicated **strip mode** that tiles trim rectangles along quad
runs at consistent texel density (the case DreamUV's island-only fitting
fails), full Valve **`.rect` round-tripping**, and an optional **predictive
atlas suggestion** pre-pass.

Built from the research plan in
[hotspot_texturing_blender_addon_plan.md](hotspot_texturing_blender_addon_plan.md).

## Install

**From source (development):**
1. Zip the `spotweld/` folder (the zip must contain the `spotweld` folder
   itself), or build properly with:
   `blender --command extension build --source-dir spotweld`
2. In Blender 5.1+: *Edit → Preferences → Get Extensions → Install from Disk…*

The panels appear in the N-panel sidebar (**SpotWeld** tab) of both the 3D
Viewport and the UV/Image Editor.

## Workflow

1. **Load an atlas** — set the texture size, then *Import* a `.rect` file,
   *Rects From Atlas Mesh* (DreamUV subrect-atlas interop), or add rects by
   hand. Full-width rects import flagged as *tiling* trims automatically. The
   rect grid draws over the image in the UV editor.
2. **Set World Scale** — the world size one full 0–1 UV tile covers; it
   bridges patch areas and rectangle sizes during matching.
3. **Select faces in Edit Mode** and hit **Fit (Auto)**: quad runs become
   tiling strips, everything else fits island-style (closest quantized aspect,
   then closest area, random tie-break — re-roll with the *Variation* redo
   setting, hold **Alt** to use alt-flagged rects). *Grow Strip* extends the
   selection along the active quad's face loop. *Keep Existing UVs* places
   without re-unwrapping: each island's current UV layout is only moved and
   scaled into the rectangle (no strip re-tiling, no random spins).
4. **Interactive Fit** (also on the toolbar): wheel cycles candidate
   rectangles, **R** re-rolls, **LMB** confirms, **RMB/Esc** cancels and
   restores the previous UVs.
   **Assign Rect (Click)** maps the selection to a rectangle you click in the
   UV editor instead of the closest match — from the panel button (waits for
   the next click; view navigation and selection still work meanwhile) or the
   *SpotWeld Assign* toolbar tool for repeated picks.
   Each rect has an overlay fill color (swatch in the list); *Color Fills*
   toggles the fills, *Opacity* fades the whole overlay, and the refresh
   button re-rolls all colors (evenly spread hues; *Seed* in the redo panel
   re-rolls again). **Double-click** a rectangle in the UV editor to make it
   the active one in the list.
5. **Atlas Prediction (optional)** — before a texture exists, *Suggest Atlas*
   measures the selection, clusters islands/strips into reusable rectangle
   buckets (Lean / Balanced / High Fidelity economy presets, or a custom
   tolerance), packs a layout, fills the rect list, and renders a labeled
   preview image. It never touches real UVs; *Export* writes the result as an
   ordinary `.rect`. With *Use Texel Density* off, sizes snap to powers of
   two and scale so the layout tiles the **entire** texture exactly — every
   leftover becomes a filler rectangle or full-width trim band (perfect
   trim-sheet coverage, padding ignored); the world scale is set from the
   chosen packing scale either way.

## Strip mode

A run of quads (detected automatically, or grown with *Grow Strip*) is
parametrized like Follow Active Quads with Length Average: u accumulates real
world length per quad, v spans the rungs. The cross-section maps to the
matched rectangle's short dimension and the run tiles along the other axis at
matching texel density — curved runs are fine; L-turn corner quads and
branches fall back to island fitting. *Snap To Whole Tiles* nudges the scale
so both strip ends land on rectangle borders.

## Module layout

| File | Role |
| --- | --- |
| `core_match.py` | `.rect` KeyValues I/O + aspect/area matching — pure Python, no `bpy` |
| `core_atlas_suggest.py` | Patch clustering + shelf packing — pure Python, no `bpy` |
| `core_geometry.py` | bmesh island split, strip detect/order/layout, projection, UV apply |
| `ops_fit.py` / `ops_select.py` | Fit operators, Grow Strip, interactive modal |
| `ops_rect_io.py` / `ops_suggest_atlas.py` | Atlas I/O and the prediction operator |
| `ui.py` / `draw.py` / `tools.py` | Panels, GPU overlays, WorkSpaceTools |

## Tests

The pure modules run headless with plain Python:

```
python tests/test_core_match.py
python tests/test_atlas_suggest.py
```

## License

GPL-3.0-or-later (required for Blender add-ons).
