# SPDX-License-Identifier: GPL-3.0-or-later
"""Hotspot fit operators. The unit-building/apply pipeline here is shared with
the interactive modal tool in ops_select.py."""

import random

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty

from . import core_geometry, core_match, draw
from .ops_rect_io import rects_from_scene

MODE_ITEMS = (
    ('AUTO', "Auto", "Quad runs become tiling strips, everything else fits as islands"),
    ('ISLAND', "Island", "Fit every island's bounding box to one rectangle (DreamUV-style)"),
    ('STRIP', "Strip", "Only fit quad strips; skip islands that aren't strips"),
)


class Unit:
    """One fittable patch (island or strip) with its ranked rect candidates."""
    __slots__ = ("kind", "obj", "uv_layer", "faces", "layout",
                 "coords", "bbox", "aspect", "area", "cands")


def get_inset_uv(st):
    return (st.inset_px / max(st.tex_width, 1), st.inset_px / max(st.tex_height, 1))


def tag_redraw_editors(context):
    wm = context.window_manager
    if not wm:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type in ('IMAGE_EDITOR', 'VIEW_3D'):
                area.tag_redraw()


def build_units(context, st, mode, use_alt):
    """Split the selection of every edit-mode mesh into islands, detect strips,
    measure patches, and rank rect candidates. Returns (units, meshes)."""
    rects = rects_from_scene(st)
    if not rects:
        return [], []

    space = context.space_data
    use_uv_select = (space is not None and space.type == 'IMAGE_EDITOR'
                     and not context.scene.tool_settings.use_uv_select_sync)
    angle = st.angle_limit if st.use_angle else None
    scale = st.world_scale

    objects = getattr(context, "objects_in_mode_unique_data", None)
    if not objects:
        objects = [context.edit_object] if context.edit_object else []

    units, meshes = [], []
    for obj in objects:
        if obj is None or obj.type != 'MESH':
            continue
        me = obj.data
        bm = bmesh.from_edit_mesh(me)
        uv_layer = bm.loops.layers.uv.verify()
        faces = core_geometry.get_target_faces(bm, uv_layer, use_uv_select)
        if not faces:
            continue
        meshes.append(me)
        mw = obj.matrix_world
        for island in core_geometry.split_islands(
                faces, st.use_seams, st.use_sharp, angle):
            u = Unit()
            u.obj = obj
            u.uv_layer = uv_layer
            u.faces = island
            u.layout = None

            if mode in ('AUTO', 'STRIP'):
                det = core_geometry.detect_strip(island)
                if det is not None:
                    u.layout = core_geometry.layout_strip(
                        det[0], det[1], det[2], mw, st.world_orient)
            if u.layout is not None:
                u.kind = 'STRIP'
                u.cands = core_match.rank_strip_rects(
                    u.layout.avg_width, rects, scale, use_alt)
            else:
                if mode == 'STRIP':
                    continue
                u.kind = 'ISLAND'
                u.coords, u.bbox = core_geometry.island_projection(island, mw)
                u.aspect = u.bbox[2] / u.bbox[3]
                u.area = sum(core_geometry.face_world_area(f, mw) for f in island)
                u.cands = core_match.rank_rects(
                    u.aspect, u.area, rects, scale,
                    st.world_orient, st.allow_flip, use_alt)
            if u.cands:
                units.append(u)
    return units, meshes


def apply_unit(u, cand, st, rng, inset_uv, reverse_strips=False):
    if u.kind == 'STRIP':
        core_geometry.apply_rect_to_strip(
            u.layout, u.uv_layer, cand.rect, inset_uv,
            rotated=cand.rotated, snap_tiles=st.snap_tiles,
            reverse_u=reverse_strips)
        return
    rot_q = 1 if cand.swap else 0
    mirror = False
    is_square = core_match.quantize_aspect(
        max(u.aspect, 1.0 / max(u.aspect, 1e-9))) == 1.0
    if not st.world_orient and is_square:
        rot_q = (rot_q + rng.randrange(4)) % 4
        mirror = rng.random() < 0.5
    if cand.rect.reflect and rng.random() < 0.5:
        mirror = not mirror
    core_geometry.apply_rect_to_island(
        u.faces, u.coords, u.bbox, u.uv_layer, cand.rect,
        inset_uv, rot_q, mirror)


class SPOTWELD_OT_fit(bpy.types.Operator):
    bl_idname = "uv.spotweld_fit"
    bl_label = "SpotWeld Fit"
    bl_description = ("Fit the selection to the closest hotspot rectangles. "
                      "Quad strips tile along their run at consistent texel "
                      "density (hold Alt to use alt-flagged rects)")
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(name="Mode", items=MODE_ITEMS, default='AUTO')
    variation: IntProperty(
        name="Variation", default=0, min=0,
        description="Re-roll random tie-breaks and mirroring (Hammer 'Fit' behaviour)")
    reverse_strips: BoolProperty(
        name="Reverse Strips", default=False,
        description="Run strip tiling in the opposite direction")
    use_alt: BoolProperty(
        name="Alt Rects", default=False,
        description="Restrict matching to alt-flagged rectangles")

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def invoke(self, context, event):
        self.use_alt = event.alt
        return self.execute(context)

    def execute(self, context):
        st = context.scene.spotweld
        if not len(st.rects):
            self.report({'ERROR'},
                        "No hotspot rectangles — import a .rect or run Suggest Atlas")
            return {'CANCELLED'}
        units, meshes = build_units(context, st, self.mode, self.use_alt)
        if not units:
            self.report({'WARNING'}, "Nothing to fit — select faces first"
                        if self.mode != 'STRIP'
                        else "No quad strips in the selection")
            return {'CANCELLED'}

        inset_uv = get_inset_uv(st)
        used = set()
        strips = 0
        for k, u in enumerate(units):
            rng = random.Random("spotweld:%d:%d" % (self.variation, k))
            if u.kind == 'STRIP':
                cand = core_match.choose_strip(u.cands, rng, st.match_margin)
                strips += 1
            else:
                cand = core_match.choose(u.cands, rng, st.match_margin)
            apply_unit(u, cand, st, rng, inset_uv, self.reverse_strips)
            used.add(cand.index)

        for me in meshes:
            bmesh.update_edit_mesh(me)
        draw.state.highlight_indices = used
        tag_redraw_editors(context)
        self.report({'INFO'}, "Fitted %d patches (%d strips)" % (len(units), strips))
        return {'FINISHED'}


classes = (SPOTWELD_OT_fit,)
