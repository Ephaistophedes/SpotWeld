# SPDX-License-Identifier: GPL-3.0-or-later
"""GPU draw handlers: rectangle-grid overlay in the UV/Image editor and strip
run paths in the 3D viewport (during the interactive tool)."""

import colorsys
import types

import blf
import bpy
import gpu
from bpy.app.handlers import persistent
from gpu_extras.batch import batch_for_shader

# Runtime-only state shared with the operators (never persisted).
state = types.SimpleNamespace(
    highlight_indices=set(),  # rect indices used by the last fit
    strip_paths=[],           # [[(x, y, z), ...], ...] while the modal runs
)

_handles = []
_error_reported = {"uv": False, "view3d": False}

# Batches live in UV space and are redrawn through a per-frame view2d
# transform, so they rebuild only when the rect list itself changes.
# LINE_STRIP/TRIS instead of LINE_LOOP/TRI_FAN: the loop/fan primitives
# misrender on the Vulkan backend (the closing loop segment is dropped).
_uv_cache = {"key": None, "items": []}    # items: [(fill, outline), ...]
_path_cache = {"ref": None, "batches": []}

QUAD_INDICES = ((0, 1, 2), (0, 2, 3))

COL_NORMAL = (0.85, 0.85, 0.85, 0.55)
COL_TILING = (0.30, 0.85, 1.00, 0.85)
COL_ALT = (1.00, 0.60, 0.20, 0.85)
COL_ACTIVE = (1.00, 1.00, 1.00, 1.00)
COL_HIGHLIGHT = (1.00, 0.85, 0.20, 1.00)
COL_HIGHLIGHT_FILL = (1.00, 0.85, 0.20, 0.15)
COL_STRIP_PATH = (0.25, 1.00, 0.55, 0.90)


def palette_color(index):
    """Distinct, stable RGB for rect `index` (golden-ratio hue walk)."""
    hue = (index * 0.61803398875) % 1.0
    return colorsys.hsv_to_rgb(hue, 0.55, 0.95)


def tag_redraw_editors(context):
    wm = context.window_manager
    if not wm:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type in ('IMAGE_EDITOR', 'VIEW_3D'):
                area.tag_redraw()


def _rect_color(r, index, active_index):
    if index in state.highlight_indices:
        return COL_HIGHLIGHT
    if index == active_index:
        return COL_ACTIVE
    if r.tiling:
        return COL_TILING
    if r.alt:
        return COL_ALT
    return COL_NORMAL


def _rect_batches(shader, st):
    key = tuple((r.umin, r.vmin, r.umax, r.vmax) for r in st.rects)
    if key != _uv_cache["key"]:
        items = []
        for umin, vmin, umax, vmax in key:
            pts = ((umin, vmin), (umax, vmin), (umax, vmax), (umin, vmax))
            fill = batch_for_shader(shader, 'TRIS', {"pos": pts},
                                    indices=QUAD_INDICES)
            outline = batch_for_shader(shader, 'LINE_STRIP',
                                       {"pos": pts + (pts[0],)})
            items.append((fill, outline))
        _uv_cache["key"] = key
        _uv_cache["items"] = items
    return _uv_cache["items"]


def _draw_uv_overlay():
    try:
        ctx = bpy.context
        st = getattr(ctx.scene, "spotweld", None)
        if st is None or not st.show_overlay or not len(st.rects):
            return
        region = ctx.region
        if region is None:
            return
        v2r = region.view2d.view_to_region
        ox, oy = v2r(0.0, 0.0, clip=False)
        sx = v2r(1.0, 0.0, clip=False)[0] - ox
        sy = v2r(0.0, 1.0, clip=False)[1] - oy

        opacity = st.overlay_opacity
        if opacity <= 0.0:
            return
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        items = _rect_batches(shader, st)
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(1.0)
        with gpu.matrix.push_pop():
            gpu.matrix.translate((ox, oy))
            gpu.matrix.scale((sx, sy))
            for i, r in enumerate(st.rects):
                fill, outline = items[i]
                if st.overlay_fill and r.color[3] > 0.0:
                    shader.uniform_float(
                        "color", (r.color[0], r.color[1], r.color[2],
                                  r.color[3] * opacity))
                    fill.draw(shader)
                if i in state.highlight_indices:
                    shader.uniform_float(
                        "color", COL_HIGHLIGHT_FILL[:3]
                        + (COL_HIGHLIGHT_FILL[3] * opacity,))
                    fill.draw(shader)
                col = _rect_color(r, i, st.active_rect_index)
                shader.uniform_float("color", col[:3] + (col[3] * opacity,))
                outline.draw(shader)
        for i, r in enumerate(st.rects):
            if (r.umax - r.umin) * sx > 26.0:
                col = _rect_color(r, i, st.active_rect_index)
                blf.size(0, 10)
                blf.color(0, col[0], col[1], col[2], opacity)
                blf.position(0, ox + r.umin * sx + 4.0,
                             oy + r.vmax * sy - 13.0, 0.0)
                blf.draw(0, str(i))
        gpu.state.blend_set('NONE')
    except Exception as ex:
        if not _error_reported["uv"]:
            _error_reported["uv"] = True
            print("SpotWeld UV overlay error (silenced hereafter):", ex)


def _draw_view3d_paths():
    try:
        paths = state.strip_paths
        if not paths:
            return
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        if paths is not _path_cache["ref"]:
            _path_cache["ref"] = paths
            _path_cache["batches"] = [
                batch_for_shader(shader, 'LINE_STRIP', {"pos": p})
                for p in paths if len(p) >= 2]
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        gpu.state.line_width_set(2.0)
        shader.uniform_float("color", COL_STRIP_PATH)
        for batch in _path_cache["batches"]:
            batch.draw(shader)
        gpu.state.line_width_set(1.0)
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.blend_set('NONE')
    except Exception as ex:
        if not _error_reported["view3d"]:
            _error_reported["view3d"] = True
            print("SpotWeld 3D overlay error (silenced hereafter):", ex)


@persistent
def _reset_state(_unused=None):
    """Fit highlights and strip paths describe the session that produced
    them — drop them when a file loads or undo rewinds past the fit."""
    state.highlight_indices = set()
    state.strip_paths = []


def register_handlers():
    _error_reported["uv"] = False
    _error_reported["view3d"] = False
    _handles.append((bpy.types.SpaceImageEditor,
                     bpy.types.SpaceImageEditor.draw_handler_add(
                         _draw_uv_overlay, (), 'WINDOW', 'POST_PIXEL')))
    _handles.append((bpy.types.SpaceView3D,
                     bpy.types.SpaceView3D.draw_handler_add(
                         _draw_view3d_paths, (), 'WINDOW', 'POST_VIEW')))
    bpy.app.handlers.load_post.append(_reset_state)
    bpy.app.handlers.undo_post.append(_reset_state)


def unregister_handlers():
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.undo_post):
        if _reset_state in handlers:
            handlers.remove(_reset_state)
    for space, handle in _handles:
        space.draw_handler_remove(handle, 'WINDOW')
    _handles.clear()
    _uv_cache["key"], _uv_cache["items"] = None, []
    _path_cache["ref"], _path_cache["batches"] = None, []
    state.highlight_indices = set()
    state.strip_paths = []
