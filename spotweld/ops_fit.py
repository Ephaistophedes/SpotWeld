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

_NO_RECTS_MSG = "No hotspot rectangles — import a .rect or run Suggest Atlas"


class Unit:
    """One fittable patch (island or strip) with its ranked rect candidates.
    Holds its source BMesh wrapper: if that Python object is garbage-collected,
    every stored BMFace/loop reference is invalidated (ReferenceError).
    `preserved` marks coords taken from the existing UV layout rather than a
    projection (Keep Existing UVs)."""
    __slots__ = ("kind", "obj", "bm", "uv_layer", "faces", "layout",
                 "coords", "bbox", "aspect", "area", "cands", "preserved")


def get_inset_uv(st):
    return (st.inset_px / max(st.tex_width, 1), st.inset_px / max(st.tex_height, 1))


def _tex_aspect(st):
    return st.tex_height / max(st.tex_width, 1)


def _rect_at(region, x, y, rects):
    """Index of the rect under region-space pixel (x, y), or -1 for a miss."""
    u, v = region.view2d.region_to_view(x, y)
    return core_match.pick_rect_at(rects, u, v)


def edit_mode_objects(context):
    """Unique-data objects in (multi-object) edit mode."""
    objects = getattr(context, "objects_in_mode_unique_data", None)
    if objects:
        return objects
    return [context.edit_object] if context.edit_object else []


def uv_select_mode(context):
    """True when the UV editor's own sync-off selection should be used."""
    space = context.space_data
    return (space is not None and space.type == 'IMAGE_EDITOR'
            and not context.scene.tool_settings.use_uv_select_sync)


def build_units(context, st, mode, use_alt):
    """Split the selection of every edit-mode mesh into islands, detect strips,
    measure patches, and rank rect candidates. Returns (units, meshes)."""
    rects = rects_from_scene(st)
    if not rects:
        return [], []

    use_uv_select = uv_select_mode(context)
    angle = st.angle_limit if st.use_angle else None
    scale = st.world_scale
    tex_aspect = _tex_aspect(st)
    preserve = st.preserve_uvs
    if preserve:
        # Keeping existing UVs implies island-style placement: strip
        # re-parametrization would rebuild the very layout it must preserve.
        mode = 'ISLAND'

    units, meshes = [], []
    for obj in edit_mode_objects(context):
        if obj is None or obj.type != 'MESH':
            continue
        me = obj.data
        bm = bmesh.from_edit_mesh(me)
        # Look before verify(): creating a UV layer on a mesh that
        # contributes no faces would permanently mutate co-edited meshes.
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None and use_uv_select:
            continue  # no UVs — nothing can be UV-selected
        faces = core_geometry.get_target_faces(bm, uv_layer, use_uv_select)
        if not faces:
            continue
        uv_layer = bm.loops.layers.uv.verify()
        # Keep (me, bm) paired: callers must hold the bm wrapper while units
        # live, or its GC invalidates every stored BMFace/loop reference.
        meshes.append((me, bm))
        mw = obj.matrix_world
        for island in core_geometry.split_islands(
                faces, st.use_seams, st.use_sharp, angle):
            u = Unit()
            u.obj = obj
            u.bm = bm
            u.uv_layer = uv_layer
            u.faces = island
            u.layout = None
            u.preserved = False

            if mode in ('AUTO', 'STRIP'):
                det = core_geometry.detect_strip(island)
                if det is not None:
                    u.layout = core_geometry.layout_strip(
                        det[0], det[1], det[2], mw, st.world_orient)
            if u.layout is not None:
                u.kind = 'STRIP'
                u.cands = core_match.rank_strip_rects(
                    u.layout.avg_width, rects, scale, use_alt,
                    tex_aspect=tex_aspect)
            else:
                if mode == 'STRIP':
                    continue
                u.kind = 'ISLAND'
                u.coords = None
                if preserve:
                    u.coords, u.bbox = core_geometry.island_uv_bounds(
                        island, uv_layer)
                    if u.coords is not None:
                        u.preserved = True
                        # UV-space aspect -> physical, same frame the rects
                        # are ranked in
                        u.aspect = (u.bbox[2] / u.bbox[3]) / tex_aspect
                if u.coords is None:  # normal path or degenerate-UV fallback
                    u.coords, u.bbox = core_geometry.island_projection(
                        island, mw)
                    u.aspect = u.bbox[2] / u.bbox[3]
                u.area = sum(core_geometry.face_world_area(f, mw) for f in island)
                u.cands = core_match.rank_rects(
                    u.aspect, u.area, rects, scale,
                    st.world_orient, st.allow_flip, use_alt,
                    tex_aspect=tex_aspect)
            if u.cands:
                units.append(u)
    return units, meshes


def apply_unit(u, cand, st, rng, inset_uv, reverse_strips=False):
    if u.kind == 'STRIP':
        core_geometry.apply_rect_to_strip(
            u.layout, u.uv_layer, cand.rect, inset_uv,
            rotated=cand.rotated, snap_tiles=st.snap_tiles,
            reverse_u=reverse_strips,
            tex_aspect=_tex_aspect(st))
        return
    rot_q = 1 if cand.swap else 0
    mirror = False
    if not u.preserved:
        # random spins/mirrors would defeat Keep Existing UVs; the swap
        # rotation above stays — it is what fits the layout into the rect
        is_square = core_match.quantize_aspect(
            max(u.aspect, 1.0 / max(u.aspect, 1e-9))) == 1.0
        if not st.world_orient and is_square:
            rot_q = (rot_q + rng.randrange(4)) % 4
            mirror = rng.random() < 0.5
        if cand.rect.reflect and rng.random() < 0.5:
            mirror = not mirror
    core_geometry.apply_rect_to_island(
        u.faces, u.coords, u.bbox, u.uv_layer, cand.rect,
        inset_uv, rot_q, mirror, rectify=not u.preserved)


def _image_editor_region(window, mx, my):
    """The IMAGE_EDITOR WINDOW region under window-space mouse coords, or None."""
    for area in window.screen.areas:
        if area.type != 'IMAGE_EDITOR':
            continue
        for region in area.regions:
            if (region.type == 'WINDOW'
                    and region.x <= mx < region.x + region.width
                    and region.y <= my < region.y + region.height):
                return region
    return None


class SPOTWELD_OT_select_rect(bpy.types.Operator):
    """Bound to double-click in the UV editor (addon keymap in tools.py)."""
    bl_idname = "uv.spotweld_select_rect"
    bl_label = "Select Rect"
    bl_description = "Make the rectangle under the cursor the active one"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        st = getattr(context.scene, "spotweld", None)
        return (st is not None and len(st.rects) > 0
                and context.space_data is not None
                and context.space_data.type == 'IMAGE_EDITOR')

    def invoke(self, context, event):
        st = context.scene.spotweld
        region = context.region
        # Pass misses through: with the overlay hidden or empty space
        # double-clicked, whatever else is bound still runs.
        if (not st.show_overlay or region is None
                or region.type != 'WINDOW'):
            return {'PASS_THROUGH'}
        idx = _rect_at(region, event.mouse_region_x, event.mouse_region_y,
                       st.rects)
        if idx < 0:
            return {'PASS_THROUGH'}
        st.active_rect_index = idx
        draw.tag_redraw_editors(context)
        return {'FINISHED'}


class SPOTWELD_OT_pick_rect(bpy.types.Operator):
    bl_idname = "uv.spotweld_pick_rect"
    bl_label = "Assign Rect (Click)"
    bl_description = ("Map the selected faces onto the rectangle you click in "
                      "the UV/Image editor. The view can still be navigated "
                      "and the selection changed while waiting (RMB/Esc "
                      "cancels)")
    bl_options = {'REGISTER', 'UNDO'}

    rect_index: IntProperty(
        name="Rectangle", default=-1, min=-1,
        description="Target rectangle index (-1 waits for a click)")
    mode: EnumProperty(name="Mode", items=MODE_ITEMS, default='AUTO')

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def invoke(self, context, event):
        st = context.scene.spotweld
        if not len(st.rects):
            self.report({'ERROR'}, _NO_RECTS_MSG)
            return {'CANCELLED'}
        space = context.space_data
        region = context.region
        if (space is not None and space.type == 'IMAGE_EDITOR'
                and region is not None and region.type == 'WINDOW'
                and event.type == 'LEFTMOUSE'):
            # Toolbar-tool click straight on the UV editor: single-shot pick.
            idx = _rect_at(region, event.mouse_region_x,
                           event.mouse_region_y, st.rects)
            if idx < 0:
                self.report({'WARNING'}, "No rectangle under the cursor")
                return {'CANCELLED'}
            self.rect_index = idx
            return self.execute(context)
        # Panel button: go modal and wait for a click in any UV/Image editor.
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set('EYEDROPPER')
        self.report({'INFO'},
                    "Click a rectangle in the UV editor (RMB/Esc cancels)")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in ('RIGHTMOUSE', 'ESC'):
            context.window.cursor_modal_restore()
            self.report({'INFO'}, "Assign cancelled")
            return {'CANCELLED'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            region = _image_editor_region(
                context.window, event.mouse_x, event.mouse_y)
            if region is not None:
                idx = _rect_at(region, event.mouse_x - region.x,
                               event.mouse_y - region.y,
                               context.scene.spotweld.rects)
                if idx < 0:
                    self.report({'WARNING'},
                                "No rectangle there — click a rectangle "
                                "(RMB/Esc cancels)")
                    return {'RUNNING_MODAL'}
                context.window.cursor_modal_restore()
                self.rect_index = idx
                return self.execute(context)
        # Everything else passes through so the view can be navigated and the
        # face selection adjusted before picking.
        return {'PASS_THROUGH'}

    def cancel(self, context):
        context.window.cursor_modal_restore()

    def execute(self, context):
        st = context.scene.spotweld
        if not (0 <= self.rect_index < len(st.rects)):
            self.report({'ERROR'}, "Rectangle index out of range")
            return {'CANCELLED'}
        target = st.rects[self.rect_index]
        units, meshes = build_units(context, st, self.mode, target.alt)
        if not units:
            self.report({'WARNING'}, "Nothing to fit — select faces first")
            return {'CANCELLED'}
        inset_uv = get_inset_uv(st)
        applied = 0
        for k, u in enumerate(units):
            cand = next((c for c in u.cands
                         if c.index == self.rect_index), None)
            if cand is None:
                continue
            rng = random.Random("spotweld:pick:%d" % k)
            apply_unit(u, cand, st, rng, inset_uv)
            applied += 1
        if not applied:
            self.report({'WARNING'}, "Selection could not be mapped to rect %d"
                        % self.rect_index)
            return {'CANCELLED'}
        for me, _bm in meshes:
            bmesh.update_edit_mesh(me)
        st.active_rect_index = self.rect_index
        draw.state.highlight_indices = {self.rect_index}
        draw.tag_redraw_editors(context)
        self.report({'INFO'}, "Assigned %d patch(es) to rect %d"
                    % (applied, self.rect_index))
        return {'FINISHED'}


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
            self.report({'ERROR'}, _NO_RECTS_MSG)
            return {'CANCELLED'}
        units, meshes = build_units(context, st, self.mode, self.use_alt)
        if not units:
            if not self.use_alt and all(r.alt for r in st.rects):
                self.report({'WARNING'}, "All rectangles are alt-flagged — "
                            "hold Alt while fitting or clear the Alt flags")
            else:
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

        for me, _bm in meshes:
            bmesh.update_edit_mesh(me)
        draw.state.highlight_indices = used
        draw.tag_redraw_editors(context)
        self.report({'INFO'}, "Fitted %d patches (%d strips)" % (len(units), strips))
        return {'FINISHED'}


classes = (SPOTWELD_OT_fit, SPOTWELD_OT_pick_rect, SPOTWELD_OT_select_rect)
