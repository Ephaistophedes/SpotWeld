# SPDX-License-Identifier: GPL-3.0-or-later
"""WorkSpaceTools exposing the interactive fit in the 3D viewport and the
UV/Image editor toolbars, plus the addon keymap (double-click selects the
rect under the cursor). Registration is best-effort — tool API details have
shifted between releases, and the operators remain reachable from the N-panel
either way."""

import bpy


class _SpotWeldToolBase:
    bl_context_mode = 'EDIT_MESH'
    bl_label = "SpotWeld Fit"
    bl_description = ("Click to hotspot-fit the selection interactively "
                      "(wheel cycles rects, R re-rolls, RMB/Esc cancels)")
    bl_icon = "ops.generic.select_box"
    bl_widget = None
    bl_keymap = (
        ("uv.spotweld_fit_interactive",
         {"type": 'LEFTMOUSE', "value": 'PRESS'}, None),
    )


class SPOTWELD_TOOL_fit_view3d(_SpotWeldToolBase, bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_idname = "spotweld.fit_tool_view3d"


class SPOTWELD_TOOL_fit_image(_SpotWeldToolBase, bpy.types.WorkSpaceTool):
    bl_space_type = 'IMAGE_EDITOR'
    # Image-editor tools are keyed by the editor's own mode, not context.mode.
    bl_context_mode = 'UV'
    bl_idname = "spotweld.fit_tool_image"


class SPOTWELD_TOOL_pick_image(bpy.types.WorkSpaceTool):
    bl_space_type = 'IMAGE_EDITOR'
    bl_context_mode = 'UV'
    bl_idname = "spotweld.pick_tool_image"
    bl_label = "SpotWeld Assign"
    bl_description = ("Click a hotspot rectangle to map the selected faces "
                      "onto it")
    bl_icon = "ops.paint.weight_sample"
    bl_widget = None
    bl_keymap = (
        ("uv.spotweld_pick_rect",
         {"type": 'LEFTMOUSE', "value": 'PRESS'}, None),
    )


_registered = []
_keymaps = []


def register():
    for cls in (SPOTWELD_TOOL_fit_view3d, SPOTWELD_TOOL_fit_image,
                SPOTWELD_TOOL_pick_image):
        try:
            bpy.utils.register_tool(cls, separator=True)
            _registered.append(cls)
        except Exception as ex:
            print("SpotWeld: could not register tool %s: %s" % (cls.__name__, ex))
    # may be None (background mode / custom builds)
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="UV Editor", space_type='EMPTY',
                            region_type='WINDOW')
        kmi = km.keymap_items.new("uv.spotweld_select_rect",
                                  'LEFTMOUSE', 'DOUBLE_CLICK')
        _keymaps.append((km, kmi))


def unregister():
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception as ex:
            print("SpotWeld: could not remove keymap item: %s" % ex)
    _keymaps.clear()
    for cls in reversed(_registered):
        try:
            bpy.utils.unregister_tool(cls)
        except Exception as ex:
            print("SpotWeld: could not unregister tool %s: %s"
                  % (cls.__name__, ex))
    _registered.clear()
