# SPDX-License-Identifier: GPL-3.0-or-later
"""Property groups, N-panels for the 3D viewport and UV/Image editor, and the
rectangle UIList."""

import math

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       FloatProperty, IntProperty)

from .core_atlas_suggest import PRESET_TOLERANCE


class SpotWeldRect(bpy.types.PropertyGroup):
    umin: FloatProperty(name="U Min", default=0.0, soft_min=0.0, soft_max=1.0)
    vmin: FloatProperty(name="V Min", default=0.0, soft_min=0.0, soft_max=1.0)
    umax: FloatProperty(name="U Max", default=0.25, soft_min=0.0, soft_max=1.0)
    vmax: FloatProperty(name="V Max", default=0.25, soft_min=0.0, soft_max=1.0)
    rotate: BoolProperty(
        name="Rotate", default=False,
        description="Region may be rotated to better match a patch")
    reflect: BoolProperty(
        name="Reflect", default=False,
        description="Region may be randomly mirrored")
    alt: BoolProperty(
        name="Alt", default=False,
        description="Alternate region — only matched while Alt is held")
    tiling: BoolProperty(
        name="Tiling", default=False,
        description="Trim region that tiles along its long axis (full-width "
                    "rects import as tiling automatically)")


class SpotWeldSettings(bpy.types.PropertyGroup):
    rects: CollectionProperty(type=SpotWeldRect)
    active_rect_index: IntProperty(default=0)

    tex_width: IntProperty(
        name="Width", default=1024, min=1, soft_max=8192,
        description="Atlas texture width in pixels (used for .rect I/O and insets)")
    tex_height: IntProperty(
        name="Height", default=1024, min=1, soft_max=8192,
        description="Atlas texture height in pixels")
    world_scale: FloatProperty(
        name="World Scale", default=2.0, min=0.001, soft_max=100.0,
        subtype='DISTANCE',
        description="World size covered by one full 0-1 UV tile — bridges "
                    "patch areas and rectangle sizes for matching")
    match_margin: FloatProperty(
        name="Match Tolerance", default=0.10, min=0.0, max=1.0,
        description="Size difference treated as a tie — tied rects are picked "
                    "randomly and re-rolled by the Variation setting")
    inset_px: FloatProperty(
        name="Inset (px)", default=0.0, min=0.0, soft_max=32.0,
        description="Shrink target rectangles by this many pixels (bevel margin)")

    world_orient: BoolProperty(
        name="World Orient", default=True,
        description="Keep world-up pointing up in the texture; disables the "
                    "random rotation of square patches")
    allow_flip: BoolProperty(
        name="Allow Flip", default=True,
        description="Wide patches may use tall rectangles and vice versa "
                    "(ignored while World Orient is on, except rects flagged Rotate)")
    snap_tiles: BoolProperty(
        name="Snap To Whole Tiles", default=True,
        description="Nudge strip tiling so a whole number of rectangle widths "
                    "covers the run, aligning both ends with the rect border")

    use_seams: BoolProperty(
        name="Seams", default=True,
        description="Seams split the selection into separate patches")
    use_sharp: BoolProperty(
        name="Sharp Edges", default=True,
        description="Sharp-marked edges split the selection into separate patches")
    use_angle: BoolProperty(
        name="Angle", default=True,
        description="Edges sharper than the angle limit split patches")
    angle_limit: FloatProperty(
        name="Angle Limit", default=math.radians(30.0), min=0.0, max=math.pi,
        subtype='ANGLE',
        description="Edge angle above which faces separate into patches")

    show_overlay: BoolProperty(
        name="Show Rect Overlay", default=True,
        description="Draw the rectangle grid over the image in the UV editor")

    # --- Atlas prediction (optional pre-pass) ---
    texel_density: FloatProperty(
        name="Texel Density", default=256.0, min=1.0, soft_max=4096.0,
        description="Target texels per world unit for suggested rectangles")
    economy: EnumProperty(
        name="Atlas Economy",
        items=(('LEAN', "Lean",
                "~30%% tolerance — fewest rectangles, most visible reuse"),
               ('BALANCED', "Balanced",
                "~15%% tolerance — recommended for modular environments"),
               ('HIFI', "High Fidelity",
                "~5%% tolerance — near one rectangle per unique patch")),
        default='BALANCED')
    use_custom_tolerance: BoolProperty(
        name="Custom Tolerance", default=False,
        description="Override the economy preset with an exact clustering tolerance")
    custom_tolerance: FloatProperty(
        name="Tolerance", default=0.15, min=0.01, max=1.0,
        description="Relative size/aspect difference merged into one bucket")
    suggest_use_strips: BoolProperty(
        name="Detect Strips", default=True,
        description="Treat quad runs as tiling trim strips (bucketed by "
                    "cross-section width only) instead of per-length islands")
    suggest_padding_px: IntProperty(
        name="Padding (px)", default=4, min=0, max=64,
        description="Gap between suggested rectangles")


def resolve_tolerance(st):
    if st.use_custom_tolerance:
        return st.custom_tolerance
    return PRESET_TOLERANCE[st.economy]


class SPOTWELD_UL_rects(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        st = data
        # abs(): min/max can be typed in either order; matching swaps them
        w = round(abs(item.umax - item.umin) * st.tex_width)
        h = round(abs(item.vmax - item.vmin) * st.tex_height)
        row = layout.row(align=True)
        row.label(text="%d:  %d × %d px" % (index, w, h))
        flags = row.row(align=True)
        flags.alignment = 'RIGHT'
        flags.prop(item, "rotate", text="", icon='FILE_REFRESH', emboss=False)
        flags.prop(item, "reflect", text="", icon='MOD_MIRROR', emboss=False)
        flags.prop(item, "alt", text="", icon='EVENT_ALT', emboss=False)
        flags.prop(item, "tiling", text="", icon='MOD_ARRAY', emboss=False)


class _SpotWeldPanelMixin:
    bl_label = "SpotWeld"
    bl_region_type = 'UI'
    bl_category = "SpotWeld"

    def draw(self, context):
        layout = self.layout
        st = context.scene.spotweld

        box = layout.box()
        box.label(text="Atlas", icon='TEXTURE')
        row = box.row(align=True)
        row.prop(st, "tex_width")
        row.prop(st, "tex_height")
        row.operator("spotweld.tex_from_image", text="", icon='IMAGE_DATA')
        box.prop(st, "world_scale")
        row = box.row(align=True)
        row.operator("spotweld.import_rect", text="Import", icon='IMPORT')
        row.operator("spotweld.export_rect", text="Export", icon='EXPORT')
        box.operator("spotweld.atlas_from_mesh", icon='MESH_GRID')
        row = box.row()
        row.template_list("SPOTWELD_UL_rects", "", st, "rects",
                          st, "active_rect_index", rows=3)
        col = row.column(align=True)
        col.operator("spotweld.rect_add", text="", icon='ADD')
        col.operator("spotweld.rect_remove", text="", icon='REMOVE')
        if 0 <= st.active_rect_index < len(st.rects):
            r = st.rects[st.active_rect_index]
            sub = box.column(align=True)
            row = sub.row(align=True)
            row.prop(r, "umin")
            row.prop(r, "vmin")
            row = sub.row(align=True)
            row.prop(r, "umax")
            row.prop(r, "vmax")
        box.prop(st, "show_overlay")

        box = layout.box()
        box.label(text="Fit", icon='UV')
        col = box.column(align=True)
        row = col.row(align=True)
        row.prop(st, "world_orient", toggle=True)
        row.prop(st, "allow_flip", toggle=True)
        col.prop(st, "inset_px")
        col.prop(st, "match_margin")
        col.prop(st, "snap_tiles")
        col = box.column(align=True)
        col.label(text="Patch Borders:")
        row = col.row(align=True)
        row.prop(st, "use_seams", toggle=True)
        row.prop(st, "use_sharp", toggle=True)
        row.prop(st, "use_angle", toggle=True)
        sub = col.row()
        sub.active = st.use_angle
        sub.prop(st, "angle_limit")
        col = box.column(align=True)
        op = col.operator("uv.spotweld_fit", text="Fit (Auto)", icon='STICKY_UVS_LOC')
        op.mode = 'AUTO'
        row = col.row(align=True)
        op = row.operator("uv.spotweld_fit", text="Islands")
        op.mode = 'ISLAND'
        op = row.operator("uv.spotweld_fit", text="Strips")
        op.mode = 'STRIP'
        box.operator("uv.spotweld_fit_interactive", icon='RESTRICT_SELECT_OFF')
        box.operator("mesh.spotweld_grow_strip", icon='SNAP_EDGE')


class SPOTWELD_PT_view3d(_SpotWeldPanelMixin, bpy.types.Panel):
    bl_idname = "SPOTWELD_PT_view3d"
    bl_space_type = 'VIEW_3D'


class SPOTWELD_PT_image(_SpotWeldPanelMixin, bpy.types.Panel):
    bl_idname = "SPOTWELD_PT_image"
    bl_space_type = 'IMAGE_EDITOR'


class _SpotWeldSuggestMixin:
    bl_label = "Atlas Prediction (optional)"
    bl_region_type = 'UI'
    bl_category = "SpotWeld"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        st = context.scene.spotweld
        col = layout.column(align=True)
        col.prop(st, "texel_density")
        row = layout.row(align=True)
        row.enabled = not st.use_custom_tolerance
        row.prop(st, "economy", expand=True)
        row = layout.row(align=True)
        row.prop(st, "use_custom_tolerance", text="", icon='TOOL_SETTINGS')
        sub = row.row()
        sub.active = st.use_custom_tolerance
        sub.prop(st, "custom_tolerance", slider=True)
        col = layout.column(align=True)
        col.prop(st, "suggest_use_strips")
        col.prop(st, "suggest_padding_px")
        layout.operator("mesh.spotweld_suggest_atlas", icon='SHADERFX')


class SPOTWELD_PT_suggest_view3d(_SpotWeldSuggestMixin, bpy.types.Panel):
    bl_idname = "SPOTWELD_PT_suggest_view3d"
    bl_space_type = 'VIEW_3D'
    bl_parent_id = "SPOTWELD_PT_view3d"


class SPOTWELD_PT_suggest_image(_SpotWeldSuggestMixin, bpy.types.Panel):
    bl_idname = "SPOTWELD_PT_suggest_image"
    bl_space_type = 'IMAGE_EDITOR'
    bl_parent_id = "SPOTWELD_PT_image"


classes = (
    SpotWeldRect,
    SpotWeldSettings,
    SPOTWELD_UL_rects,
    SPOTWELD_PT_view3d,
    SPOTWELD_PT_image,
    SPOTWELD_PT_suggest_view3d,
    SPOTWELD_PT_suggest_image,
)
