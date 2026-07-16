# SPDX-License-Identifier: GPL-3.0-or-later
"""Predictive atlas suggestion operator: measure the selection's islands and
strips, cluster them into reusable rectangle buckets, shelf-pack a layout,
fill the scene rect list, and render a labeled preview texture.

Never touches real mesh UVs — advisory only, re-runnable any time."""

import colorsys

import bmesh
import bpy
from bpy.props import BoolProperty
from mathutils import Matrix

from . import core_atlas_suggest, core_geometry, core_match, ui
from .ops_rect_io import rects_to_scene

PREVIEW_IMAGE_NAME = "SpotWeld_AtlasPreview"


def _extract_patches(bm, faces, mw, st):
    """Measure islands/strips in a face set into core_atlas_suggest Patches."""
    angle = st.angle_limit if st.use_angle else None
    patches = []
    for island in core_geometry.split_islands(
            faces, st.use_seams, st.use_sharp, angle):
        layout = None
        if st.suggest_use_strips:
            det = core_geometry.detect_strip(island)
            if det is not None:
                layout = core_geometry.layout_strip(det[0], det[1], det[2], mw)
        if layout is not None:
            patches.append(core_atlas_suggest.Patch(
                'STRIP', layout.total_len, layout.avg_width,
                length=layout.total_len))
        else:
            _coords, bbox = core_geometry.island_projection(island, mw)
            area = sum(core_geometry.face_world_area(f, mw) for f in island)
            long_side = max(bbox[2], bbox[3])
            short_side = min(bbox[2], bbox[3])
            patches.append(core_atlas_suggest.Patch(
                'ISLAND', long_side, short_side, area=area))
    return patches


def _bucket_color(i):
    h = (i * 0.381966) % 1.0  # golden-angle hue walk
    r, g, b = colorsys.hsv_to_rgb(h, 0.55, 0.82)
    return (r, g, b, 1.0)


def _preview_gpu(tex_w, tex_h, placements):
    """Render labeled flat-color rects into a pixel buffer via GPU offscreen."""
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader

    offscreen = gpu.types.GPUOffScreen(tex_w, tex_h)
    try:
        with offscreen.bind():
            fb = gpu.state.active_framebuffer_get()
            fb.clear(color=(0.12, 0.12, 0.13, 1.0))
            with gpu.matrix.push_pop():
                gpu.matrix.load_matrix(Matrix.Identity(4))
                gpu.matrix.load_projection_matrix(Matrix((
                    (2.0 / tex_w, 0.0, 0.0, -1.0),
                    (0.0, 2.0 / tex_h, 0.0, -1.0),
                    (0.0, 0.0, -1.0, 0.0),
                    (0.0, 0.0, 0.0, 1.0))))
                shader = gpu.shader.from_builtin('UNIFORM_COLOR')
                for i, (b, w, h, full, x, y) in enumerate(placements):
                    col = _bucket_color(i)
                    pts = ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
                    batch = batch_for_shader(shader, 'TRI_FAN', {"pos": pts})
                    shader.uniform_float("color", col)
                    batch.draw(shader)
                    outline = batch_for_shader(shader, 'LINE_LOOP', {"pos": pts})
                    shader.uniform_float("color",
                                         (col[0] * 0.4, col[1] * 0.4, col[2] * 0.4, 1.0))
                    outline.draw(shader)
                    if w >= 48 and h >= 16:
                        label = "%d  %dx%d px" % (i, w, h)
                        label += "  tile" if full else "  x%d" % len(b.members)
                        size = min(26, max(11, int(h * 0.5)))
                        blf.size(0, size)
                        blf.color(0, 0.05, 0.05, 0.05, 0.9)
                        blf.position(0, x + 6, y + (h - size) * 0.5 + 2, 0)
                        blf.draw(0, label)
            buffer = fb.read_color(0, 0, tex_w, tex_h, 4, 0, 'FLOAT')
    finally:
        offscreen.free()
    buffer.dimensions = tex_w * tex_h * 4
    return buffer


def _preview_numpy(tex_w, tex_h, placements):
    """Flat-color fallback (no labels) for contexts without a GPU."""
    import numpy as np
    arr = np.empty((tex_h, tex_w, 4), dtype=np.float32)
    arr[:] = (0.12, 0.12, 0.13, 1.0)
    for i, (_b, w, h, _full, x, y) in enumerate(placements):
        y1 = min(y + h, tex_h)
        x1 = min(x + w, tex_w)
        if y < tex_h and x < tex_w:
            arr[y:y1, x:x1] = _bucket_color(i)
    return arr.ravel()


def _make_preview_image(tex_w, tex_h, placements):
    try:
        pixels = _preview_gpu(tex_w, tex_h, placements)
    except Exception as ex:
        print("SpotWeld: GPU preview failed (%s), using flat fallback" % ex)
        pixels = _preview_numpy(tex_w, tex_h, placements)
    img = bpy.data.images.get(PREVIEW_IMAGE_NAME)
    if img is not None and tuple(img.size) != (tex_w, tex_h):
        bpy.data.images.remove(img)
        img = None
    if img is None:
        img = bpy.data.images.new(PREVIEW_IMAGE_NAME, tex_w, tex_h, alpha=False)
    img.pixels.foreach_set(pixels)
    img.update()
    return img


class SPOTWELD_OT_suggest_atlas(bpy.types.Operator):
    bl_idname = "mesh.spotweld_suggest_atlas"
    bl_label = "Suggest Atlas"
    bl_description = ("Analyze the selection, cluster its islands and strips "
                      "into reusable rectangle buckets, and propose a starting "
                      "atlas layout + labeled preview texture. Never modifies "
                      "mesh UVs")
    bl_options = {'REGISTER', 'UNDO'}

    replace: BoolProperty(
        name="Replace Rect List", default=True,
        description="Replace the current rectangles with the suggestion")
    show_preview: BoolProperty(
        name="Show Preview In UV Editor", default=True,
        description="Assign the preview texture to open UV/Image editors")

    @classmethod
    def poll(cls, context):
        if context.mode == 'EDIT_MESH':
            return True
        return any(ob.type == 'MESH' for ob in context.selected_objects)

    def execute(self, context):
        st = context.scene.spotweld
        tol = ui.resolve_tolerance(st)
        tex_w, tex_h = st.tex_width, st.tex_height
        density = st.texel_density

        patches = []
        if context.mode == 'EDIT_MESH':
            objects = getattr(context, "objects_in_mode_unique_data", None) or \
                ([context.edit_object] if context.edit_object else [])
            for obj in objects:
                if obj is None or obj.type != 'MESH':
                    continue
                bm = bmesh.from_edit_mesh(obj.data)
                faces = [f for f in bm.faces if f.select and not f.hide]
                patches.extend(_extract_patches(bm, faces, obj.matrix_world, st))
        else:
            for obj in context.selected_objects:
                if obj.type != 'MESH':
                    continue
                bm = bmesh.new()
                bm.from_mesh(obj.data)
                patches.extend(_extract_patches(bm, list(bm.faces),
                                                obj.matrix_world, st))
                bm.free()
        if not patches:
            self.report({'ERROR'}, "Nothing to analyze — select faces or mesh objects")
            return {'CANCELLED'}

        buckets = core_atlas_suggest.cluster_patches(patches, tol)
        sized, clamped = core_atlas_suggest.size_buckets(
            buckets, density, tex_w, tex_h)
        placements, used_h = core_atlas_suggest.shelf_pack(
            sized, tex_w, tex_h, st.suggest_padding_px)

        rects = []
        for b, w, h, full, x, y in placements:
            r = core_match.Rect(x / tex_w, y / tex_h,
                                (x + w) / tex_w, (y + h) / tex_h,
                                tiling=full)
            rects.append(r)
        rects_to_scene(st, rects, self.replace)

        # One full UV tile now spans tex_w px at `density` px/unit:
        st.world_scale = tex_w / density

        img = _make_preview_image(tex_w, tex_h, placements)
        if self.show_preview and img is not None:
            for area in context.screen.areas:
                if area.type == 'IMAGE_EDITOR':
                    area.spaces.active.image = img
                    area.tag_redraw()

        usage = 100.0 * min(used_h, tex_h) / max(tex_h, 1)
        msg = ("%d patches -> %d rectangles, ~%d%% of a %dx%d atlas"
               % (len(patches), len(buckets), round(usage), tex_w, tex_h))
        if used_h > tex_h:
            self.report({'WARNING'}, msg + " — OVERFLOW: raise texture size "
                        "or lower texel density")
        elif clamped:
            self.report({'WARNING'}, msg + " (some patches were larger than "
                        "the texture and were clamped)")
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


classes = (SPOTWELD_OT_suggest_atlas,)
