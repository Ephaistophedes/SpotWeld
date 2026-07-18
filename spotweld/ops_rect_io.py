# SPDX-License-Identifier: GPL-3.0-or-later
"""Rect atlas I/O operators: Valve .rect import/export, DreamUV atlas-mesh
import, texture-size sync, and manual rect list editing."""

import random

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import core_match, draw


# ---------------------------------------------------------------------------
# Scene <-> core conversion
# ---------------------------------------------------------------------------

def rects_from_scene(st):
    return [core_match.Rect(r.umin, r.vmin, r.umax, r.vmax,
                            rotate=r.rotate, reflect=r.reflect,
                            alt=r.alt, tiling=r.tiling, name=r.name)
            for r in st.rects]


def rects_to_scene(st, rects, replace=True):
    if replace:
        st.rects.clear()
    for r in rects:
        item = st.rects.add()
        item.name = r.name
        item.umin, item.vmin = r.umin, r.vmin
        item.umax, item.vmax = r.umax, r.vmax
        item.rotate, item.reflect = r.rotate, r.reflect
        item.alt, item.tiling = r.alt, r.tiling
        item.color = draw.palette_color(len(st.rects) - 1) + (item.color[3],)
    st.active_rect_index = min(st.active_rect_index, max(len(st.rects) - 1, 0))
    draw.state.highlight_indices = set()


# ---------------------------------------------------------------------------
# .rect import / export
# ---------------------------------------------------------------------------

class SPOTWELD_OT_import_rect(bpy.types.Operator, ImportHelper):
    bl_idname = "spotweld.import_rect"
    bl_label = "Import .rect"
    bl_description = "Load a Valve/Hammer++ .rect hotspot atlas (pixel coords are read against the Atlas texture size)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".rect"
    filter_glob: StringProperty(default="*.rect", options={'HIDDEN'})
    replace: BoolProperty(
        name="Replace Existing", default=True,
        description="Clear the current rectangle list before importing")

    def execute(self, context):
        st = context.scene.spotweld
        try:
            with open(self.filepath, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as ex:
            self.report({'ERROR'}, "Cannot read file: %s" % ex)
            return {'CANCELLED'}
        rects = core_match.parse_rect(text, st.tex_width, st.tex_height)
        if not rects:
            self.report({'ERROR'}, "No rectangles found in file")
            return {'CANCELLED'}
        rects_to_scene(st, rects, self.replace)
        draw.tag_redraw_editors(context)
        self.report({'INFO'}, "Imported %d rectangles (%d×%d px)"
                    % (len(rects), st.tex_width, st.tex_height))
        return {'FINISHED'}


class SPOTWELD_OT_export_rect(bpy.types.Operator, ExportHelper):
    bl_idname = "spotweld.export_rect"
    bl_label = "Export .rect"
    bl_description = "Write the rectangle list as a Valve/Hammer++ .rect file"

    filename_ext = ".rect"
    filter_glob: StringProperty(default="*.rect", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        st = getattr(context.scene, "spotweld", None)
        return st is not None and len(st.rects) > 0

    def execute(self, context):
        st = context.scene.spotweld
        rects = rects_from_scene(st)
        text = core_match.export_rect(rects, st.tex_width, st.tex_height)
        try:
            with open(self.filepath, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        except OSError as ex:
            self.report({'ERROR'}, "Cannot write file: %s" % ex)
            return {'CANCELLED'}
        msg = "Exported %d rectangles to %s" % (len(rects), self.filepath)
        # The Valve format expresses tiling only as full-width rects; any
        # manually flagged narrower rect loses the flag on reimport.
        lost = sum(1 for r in rects if r.tiling and not r.is_full_width())
        if lost:
            self.report({'WARNING'}, msg + " — %d Tiling flag(s) on "
                        "non-full-width rects have no .rect representation "
                        "and will be lost on reimport" % lost)
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# DreamUV atlas-mesh interop & helpers
# ---------------------------------------------------------------------------

class SPOTWELD_OT_atlas_from_mesh(bpy.types.Operator):
    bl_idname = "spotweld.atlas_from_mesh"
    bl_label = "Rects From Atlas Mesh"
    bl_description = "Read the active object's face UV bounding boxes as rectangles (DreamUV subrect-atlas interop)"
    bl_options = {'REGISTER', 'UNDO'}

    replace: BoolProperty(name="Replace Existing", default=True)

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return ob is not None and ob.type == 'MESH' and len(ob.data.uv_layers) > 0

    def execute(self, context):
        st = context.scene.spotweld
        ob = context.active_object
        if ob.mode == 'EDIT':
            ob.update_from_editmode()
        me = ob.data
        uvs = me.uv_layers.active.uv
        rects = []
        seen = set()
        for poly in me.polygons:
            coords = [uvs[li].vector for li in poly.loop_indices]
            umin = min(c.x for c in coords)
            umax = max(c.x for c in coords)
            vmin = min(c.y for c in coords)
            vmax = max(c.y for c in coords)
            key = (round(umin, 5), round(vmin, 5), round(umax, 5), round(vmax, 5))
            if key in seen or umax - umin < 1e-6 or vmax - vmin < 1e-6:
                continue
            seen.add(key)
            r = core_match.Rect(umin, vmin, umax, vmax)
            r.tiling = r.is_full_width()
            rects.append(r)
        if not rects:
            self.report({'ERROR'}, "No usable face UV rectangles on %s" % ob.name)
            return {'CANCELLED'}
        rects_to_scene(st, rects, self.replace)
        draw.tag_redraw_editors(context)
        self.report({'INFO'}, "Read %d rectangles from %s" % (len(rects), ob.name))
        return {'FINISHED'}


class SPOTWELD_OT_tex_from_image(bpy.types.Operator):
    bl_idname = "spotweld.tex_from_image"
    bl_label = "Size From Image"
    bl_description = "Set the atlas texture size from the image open in a UV/Image editor"

    def execute(self, context):
        st = context.scene.spotweld
        img = None
        space = context.space_data
        if space is not None and space.type == 'IMAGE_EDITOR' and space.image:
            img = space.image
        else:
            for area in context.screen.areas:
                if area.type == 'IMAGE_EDITOR' and area.spaces.active.image:
                    img = area.spaces.active.image
                    break
        if img is None or img.size[0] == 0:
            self.report({'ERROR'}, "No image open in a UV/Image editor")
            return {'CANCELLED'}
        st.tex_width, st.tex_height = img.size[0], img.size[1]
        self.report({'INFO'}, "Atlas size set to %d×%d" % (img.size[0], img.size[1]))
        return {'FINISHED'}


class SPOTWELD_OT_rect_add(bpy.types.Operator):
    bl_idname = "spotweld.rect_add"
    bl_label = "Add Rectangle"
    bl_description = "Add a rectangle to the atlas list"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        st = context.scene.spotweld
        item = st.rects.add()
        item.umin, item.vmin, item.umax, item.vmax = 0.0, 0.75, 0.25, 1.0
        item.color = draw.palette_color(len(st.rects) - 1) + (item.color[3],)
        st.active_rect_index = len(st.rects) - 1
        draw.tag_redraw_editors(context)
        return {'FINISHED'}


class SPOTWELD_OT_rect_colors_randomize(bpy.types.Operator):
    bl_idname = "spotweld.rect_colors_randomize"
    bl_label = "Randomize Colors"
    bl_description = ("Give every rectangle a fresh random overlay color "
                      "(evenly spread hues; fill alpha is kept). Re-roll "
                      "from the redo panel's Seed")
    bl_options = {'REGISTER', 'UNDO'}

    seed: IntProperty(
        name="Seed", default=0, min=0,
        description="Random seed for the color set")

    @classmethod
    def poll(cls, context):
        st = getattr(context.scene, "spotweld", None)
        return st is not None and len(st.rects) > 0

    def invoke(self, context, event):
        self.seed = random.randrange(1 << 16)
        return self.execute(context)

    def execute(self, context):
        st = context.scene.spotweld
        rng = random.Random(self.seed)
        # Rotate the golden-ratio palette by a random offset and jitter
        # saturation/value per rect: colors stay evenly spread (no two
        # neighbors alike) while every roll looks different.
        offset = rng.random()
        for i, r in enumerate(st.rects):
            rgb = draw.palette_color(i, offset,
                                     sat=0.40 + 0.40 * rng.random(),
                                     val=0.70 + 0.30 * rng.random())
            r.color = rgb + (r.color[3],)
        draw.tag_redraw_editors(context)
        return {'FINISHED'}


class SPOTWELD_OT_rect_remove(bpy.types.Operator):
    bl_idname = "spotweld.rect_remove"
    bl_label = "Remove Rectangle"
    bl_description = "Remove the selected rectangle from the atlas list"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        st = getattr(context.scene, "spotweld", None)
        return st is not None and 0 <= st.active_rect_index < len(st.rects)

    def execute(self, context):
        st = context.scene.spotweld
        st.rects.remove(st.active_rect_index)
        st.active_rect_index = min(st.active_rect_index, len(st.rects) - 1)
        draw.state.highlight_indices = set()
        draw.tag_redraw_editors(context)
        return {'FINISHED'}


classes = (
    SPOTWELD_OT_import_rect,
    SPOTWELD_OT_export_rect,
    SPOTWELD_OT_atlas_from_mesh,
    SPOTWELD_OT_tex_from_image,
    SPOTWELD_OT_rect_add,
    SPOTWELD_OT_rect_remove,
    SPOTWELD_OT_rect_colors_randomize,
)
