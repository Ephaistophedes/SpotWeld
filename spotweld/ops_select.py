# SPDX-License-Identifier: GPL-3.0-or-later
"""Strip selection helper and the interactive modal fit tool."""

import random

import bmesh
import bpy

from . import core_geometry, draw, ops_fit


class SPOTWELD_OT_grow_strip(bpy.types.Operator):
    bl_idname = "mesh.spotweld_grow_strip"
    bl_label = "Grow Strip"
    bl_description = ("Extend the selection from the active quad along its "
                      "face loop in both directions, stopping at seams, sharp "
                      "edges, non-quads, and branches")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        st = context.scene.spotweld

        def delimit(e):
            if len(e.link_faces) != 2:
                return True
            if st.use_seams and e.seam:
                return True
            if st.use_sharp and not e.smooth:
                return True
            if st.use_angle and e.calc_face_angle(3.15) > st.angle_limit:
                return True
            return False

        grown_any = False
        for obj in ops_fit.edit_mode_objects(context):
            if obj is None or obj.type != 'MESH':
                continue
            me = obj.data
            bm = bmesh.from_edit_mesh(me)
            seed = bm.faces.active
            if seed is None or not seed.select or len(seed.verts) != 4:
                seed = next((f for f in bm.faces
                             if f.select and len(f.verts) == 4), None)
            if seed is None:
                continue

            # Two opposite-edge pairs = two possible run directions; grow the
            # longer combined run.
            edges = list(seed.edges)
            e0 = edges[0]
            e0_opp = core_geometry._opposite_edge(seed, e0)
            pair_a = [e for e in (e0, e0_opp) if e is not None]
            pair_b = [e for e in edges if e not in pair_a]
            best = []
            for pair in (pair_a, pair_b):
                run = []
                for e in pair:
                    run.extend(core_geometry.walk_run(seed, e, delimit))
                if len(run) > len(best):
                    best = run
            if not best:
                continue
            for f in best:
                f.select = True
            bm.select_flush_mode()
            bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
            grown_any = True

        if not grown_any:
            self.report({'WARNING'}, "No quad run found from the active face")
            return {'CANCELLED'}
        return {'FINISHED'}


class SPOTWELD_OT_fit_interactive(bpy.types.Operator):
    bl_idname = "uv.spotweld_fit_interactive"
    bl_label = "SpotWeld Interactive Fit"
    bl_description = ("Fit interactively: mouse wheel cycles candidate "
                      "rectangles, R re-rolls variations, T turns islands "
                      "inside their rect (Shift+T back), LMB confirms, "
                      "RMB/Esc cancels and restores UVs")
    bl_options = {'REGISTER', 'UNDO'}

    _PASS_THROUGH = {
        'MIDDLEMOUSE', 'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE',
        'TRACKPADPAN', 'TRACKPADZOOM', 'NDOF_MOTION',
    }

    @classmethod
    def poll(cls, context):
        st = getattr(context.scene, "spotweld", None)
        return (context.mode == 'EDIT_MESH'
                and st is not None and len(st.rects) > 0)

    def invoke(self, context, event):
        st = context.scene.spotweld
        self._units, self._meshes = ops_fit.build_units(
            context, st, 'AUTO', event.alt)
        if not self._units:
            if not event.alt and all(r.alt for r in st.rects):
                self.report({'WARNING'}, "All rectangles are alt-flagged — "
                            "hold Alt while invoking or clear the Alt flags")
            else:
                self.report({'WARNING'}, "Nothing to fit — select faces first")
            return {'CANCELLED'}

        self._backup = []
        for u in self._units:
            self._backup.append(
                (u.uv_layer, core_geometry.backup_uvs(u.faces, u.uv_layer)))
        self._idx = 0
        self._variation = 0
        self._turn = 0

        draw.state.strip_paths = [
            [tuple(u.obj.matrix_world @ f.calc_center_median())
             for f in u.layout.faces]
            for u in self._units if u.kind == 'STRIP']

        self._apply(context)
        context.window_manager.modal_handler_add(self)
        if context.area:
            context.area.header_text_set(
                "SpotWeld: Wheel = cycle rects | R = re-roll | "
                "T = turn in rect (Shift+T back) | "
                "LMB = confirm | RMB/Esc = cancel")
        return {'RUNNING_MODAL'}

    def _apply(self, context):
        st = context.scene.spotweld
        inset_uv = ops_fit.get_inset_uv(st)
        used = set()
        for k, u in enumerate(self._units):
            cand = u.cands[self._idx % len(u.cands)]
            rng = random.Random("spotweld-modal:%d:%d" % (self._variation, k))
            ops_fit.apply_unit(u, cand, st, rng, inset_uv,
                               extra_quarters=self._turn)
            used.add(cand.index)
        for me, _bm in self._meshes:
            bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
        draw.state.highlight_indices = used
        draw.tag_redraw_editors(context)

    def _restore(self):
        try:
            for uv_layer, backup in self._backup:
                core_geometry.restore_uvs(backup, uv_layer)
            for me, _bm in self._meshes:
                bmesh.update_edit_mesh(me, loop_triangles=False,
                                       destructive=False)
        except ReferenceError:
            pass  # mesh data invalidated behind us — nothing to restore

    def _cleanup(self, context):
        draw.state.strip_paths = []
        if context.area:
            context.area.header_text_set(None)
        draw.tag_redraw_editors(context)

    def cancel(self, context):
        # Blender force-terminates the modal (file load, window close):
        # restore what we can and drop all overlay state.
        self._restore()
        draw.state.highlight_indices = set()
        self._cleanup(context)

    def modal(self, context, event):
        if context.mode != 'EDIT_MESH':
            # The edit bmesh is gone, so the applied preview can't be
            # restored — keep it and end as a confirm, not a false cancel.
            self.report({'INFO'}, "SpotWeld: edit mode ended — applied fit kept")
            self._cleanup(context)
            return {'FINISHED'}
        if event.type in self._PASS_THROUGH:
            return {'PASS_THROUGH'}
        if event.type in ('WHEELUPMOUSE', 'WHEELDOWNMOUSE'):
            if event.ctrl or event.alt:
                return {'PASS_THROUGH'}  # keep ctrl-wheel zoom etc. working
            if event.value != 'RELEASE':
                self._idx += 1 if event.type == 'WHEELUPMOUSE' else -1
                self._apply(context)
            return {'RUNNING_MODAL'}
        if event.type == 'R' and event.value == 'PRESS':
            self._variation += 1
            self._apply(context)
            return {'RUNNING_MODAL'}
        if event.type == 'T' and event.value == 'PRESS':
            self._turn = (self._turn + (-1 if event.shift else 1)) % 4
            self._apply(context)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self._cleanup(context)
            return {'FINISHED'}
        if event.type in ('RIGHTMOUSE', 'ESC') and event.value == 'PRESS':
            self._restore()
            draw.state.highlight_indices = set()
            self._cleanup(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


classes = (
    SPOTWELD_OT_grow_strip,
    SPOTWELD_OT_fit_interactive,
)
