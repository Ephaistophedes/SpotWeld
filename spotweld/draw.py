# SPDX-License-Identifier: GPL-3.0-or-later
"""GPU draw handlers: rectangle-grid overlay in the UV/Image editor and strip
run paths in the 3D viewport (during the interactive tool)."""

import types

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

# Runtime-only state shared with the operators (never persisted).
state = types.SimpleNamespace(
    highlight_indices=set(),  # rect indices used by the last fit
    strip_paths=[],           # [[(x, y, z), ...], ...] while the modal runs
)

_handles = []
_error_reported = [False]

COL_NORMAL = (0.85, 0.85, 0.85, 0.55)
COL_TILING = (0.30, 0.85, 1.00, 0.85)
COL_ALT = (1.00, 0.60, 0.20, 0.85)
COL_ACTIVE = (1.00, 1.00, 1.00, 1.00)
COL_HIGHLIGHT = (1.00, 0.85, 0.20, 1.00)
COL_HIGHLIGHT_FILL = (1.00, 0.85, 0.20, 0.15)
COL_STRIP_PATH = (0.25, 1.00, 0.55, 0.90)


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

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(1.0)
        for i, r in enumerate(st.rects):
            pts = (v2r(r.umin, r.vmin, clip=False),
                   v2r(r.umax, r.vmin, clip=False),
                   v2r(r.umax, r.vmax, clip=False),
                   v2r(r.umin, r.vmax, clip=False))
            highlighted = i in state.highlight_indices
            if highlighted:
                fill = batch_for_shader(shader, 'TRI_FAN', {"pos": pts})
                shader.uniform_float("color", COL_HIGHLIGHT_FILL)
                fill.draw(shader)
                col = COL_HIGHLIGHT
            elif i == st.active_rect_index:
                col = COL_ACTIVE
            elif r.tiling:
                col = COL_TILING
            elif r.alt:
                col = COL_ALT
            else:
                col = COL_NORMAL
            outline = batch_for_shader(shader, 'LINE_LOOP', {"pos": pts})
            shader.uniform_float("color", col)
            outline.draw(shader)

            if pts[1][0] - pts[0][0] > 26.0:
                blf.size(0, 10)
                blf.color(0, col[0], col[1], col[2], 1.0)
                blf.position(0, pts[3][0] + 4.0, pts[3][1] - 13.0, 0.0)
                blf.draw(0, str(i))
        gpu.state.blend_set('NONE')
    except Exception as ex:
        if not _error_reported[0]:
            _error_reported[0] = True
            print("SpotWeld UV overlay error (silenced hereafter):", ex)


def _draw_view3d_paths():
    try:
        if not state.strip_paths:
            return
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        gpu.state.line_width_set(2.0)
        shader.uniform_float("color", COL_STRIP_PATH)
        for path in state.strip_paths:
            if len(path) < 2:
                continue
            batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": path})
            batch.draw(shader)
        gpu.state.line_width_set(1.0)
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.blend_set('NONE')
    except Exception as ex:
        if not _error_reported[0]:
            _error_reported[0] = True
            print("SpotWeld 3D overlay error (silenced hereafter):", ex)


def register_handlers():
    _handles.append((bpy.types.SpaceImageEditor,
                     bpy.types.SpaceImageEditor.draw_handler_add(
                         _draw_uv_overlay, (), 'WINDOW', 'POST_PIXEL')))
    _handles.append((bpy.types.SpaceView3D,
                     bpy.types.SpaceView3D.draw_handler_add(
                         _draw_view3d_paths, (), 'WINDOW', 'POST_VIEW')))


def unregister_handlers():
    for space, handle in _handles:
        space.draw_handler_remove(handle, 'WINDOW')
    _handles.clear()
    state.highlight_indices = set()
    state.strip_paths = []
