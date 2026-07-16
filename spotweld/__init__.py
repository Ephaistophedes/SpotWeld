# SPDX-License-Identifier: GPL-3.0-or-later
"""SpotWeld — strip-aware hotspot UV texturing for Blender 5.1+.

Fits selected geometry to the closest rectangles of a hotspot atlas
(Hammer++/Source 2 style), with a dedicated strip mode that tiles trim
rectangles along quad runs at consistent texel density, .rect round-tripping,
and an optional predictive atlas suggestion pre-pass."""

_needs_reload = "core_match" in locals()

from . import (  # noqa: E402
    core_match,
    core_atlas_suggest,
    core_geometry,
    draw,
    ui,
    ops_rect_io,
    ops_fit,
    ops_select,
    ops_suggest_atlas,
    tools,
)

if _needs_reload:
    import importlib

    core_match = importlib.reload(core_match)
    core_atlas_suggest = importlib.reload(core_atlas_suggest)
    core_geometry = importlib.reload(core_geometry)
    draw = importlib.reload(draw)
    ui = importlib.reload(ui)
    ops_rect_io = importlib.reload(ops_rect_io)
    ops_fit = importlib.reload(ops_fit)
    ops_select = importlib.reload(ops_select)
    ops_suggest_atlas = importlib.reload(ops_suggest_atlas)
    tools = importlib.reload(tools)

import bpy  # noqa: E402

_class_modules = (ui, ops_rect_io, ops_fit, ops_select, ops_suggest_atlas)


def _classes():
    out = []
    for module in _class_modules:
        out.extend(getattr(module, "classes", ()))
    return out


def register():
    for cls in _classes():
        bpy.utils.register_class(cls)
    bpy.types.Scene.spotweld = bpy.props.PointerProperty(type=ui.SpotWeldSettings)
    draw.register_handlers()
    tools.register()


def unregister():
    tools.unregister()
    draw.unregister_handlers()
    del bpy.types.Scene.spotweld
    for cls in reversed(_classes()):
        bpy.utils.unregister_class(cls)
