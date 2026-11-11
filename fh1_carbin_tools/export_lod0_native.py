"""Dev21 native attached-geometry writer (split-vertex donor fix)."""
import hashlib
import math
import struct
from pathlib import Path

from . import import_lod0, damage_donor


def export_model(active, destination):
    import bpy
    from mathutils import Matrix, Vector
    source = Path(str(active.get('fh1_export_source_path', ''))).resolve()
    if not source.is_file():
        raise ValueError('Reimport LOD0/cockpit using Dev14 to enable native export')
    original = source.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest != active.get('fh1_export_source_sha256'):
        raise ValueError('Original CARBIN has changed since import')
    dest = Path(destination).resolve()
    if dest == source:
        raise ValueError('Refusing to overwrite source CARBIN')
    blocks, _ = import_lod0.parse(source)
    by_offset = {b['offset']: b for b in blocks}
    objects = [o for o in bpy.data.objects if o.type == 'MESH'
               and o.get('fh1_export_source_path') == str(source)
               and o.get('fh1_export_source_sha256') == digest]
    # Importer stores source header offset on the component collection, while
    # mesh objects carry the equivalent native data offset (+20 bytes).
    def source_header_offset(obj):
        offset = obj.get('fh1_native_vertex_data_offset')
        if offset is not None:
            return int(offset) - 20
        for collection in obj.users_collection:
            value = collection.get('fh1_source_vertex_header_offset')
            if value is not None:
                return int(value)
        raise ValueError('Missing native source vertex offset on ' + obj.name)

    offsets = [source_header_offset(o) for o in objects]
    if len(objects) != len(blocks) or len(set(offsets)) != len(offsets) or set(offsets) != set(by_offset):
        raise ValueError('Missing/duplicated imported components; reimport entire model')
    # Rebuild all insertions against ORIGINAL offsets; never mutate a buffer and
    # then interpret stale absolute addresses against the shifted file.
    insertions = []
    replacements = []
    out = bytearray(original)
    added_vertices = 0
    added_faces = 0
    index_width = 2 if struct.unpack_from('>I', original, 0)[0] == 1 else 4
    restart = import_lod0.RESTART_16 if index_width == 2 else import_lod0.RESTART
    inv = Matrix.Rotation(-math.pi / 2, 3, 'X')
    edited = 0
    worst_error = 0.0
    # Native components use the same normalized-extrema mapping as LOD1-5.
    # Rebuild whole affected buffers, never clamp individual edited vertices.
    header_owners = {}
    for obj in objects:
        off = int(obj['fh1_position_header_offset'])
        header_owners.setdefault(off, []).append(obj.name)
    if any(len(names) != 1 for names in header_owners.values()):
        raise ValueError('Native position header shared by multiple components; coordinated bounds rebuilding required: %s' %
                         {k: v for k, v in header_owners.items() if len(v) > 1})
    for obj in objects:
        block = by_offset[source_header_offset(obj)]
        mesh = obj.data
        ids = mesh.attributes.get('fh1_original_vertex_id')
        face_ids = mesh.attributes.get('fh1_original_section_id')
        if ids is None or face_ids is None:
            raise ValueError('Missing source provenance on ' + obj.name)
        if len(ids.data) != len(mesh.vertices) or len(face_ids.data) != len(mesh.polygons):
            raise ValueError('Invalid provenance attributes on ' + obj.name)
        new_verts = [v.index for v in mesh.vertices if ids.data[v.index].value == 0]
        new_polys = [poly for poly in mesh.polygons if face_ids.data[poly.index].value == 0]
        if any(face_ids.data[p.index].value != 0 and any(ids.data[i].value == 0 for i in p.vertices)
               for p in mesh.polygons):
            raise ValueError('New vertices in original faces on %s; attach as separate new faces' % obj.name)
        if len(mesh.polygons) != int(obj.get('fh1_imported_face_count', -1)) + len(new_polys):
            raise ValueError('Original faces deleted or replaced on ' + obj.name)
        if len(mesh.vertices) != int(obj.get('fh1_imported_vertex_count', -1)) + len(new_verts):
            raise ValueError('Original vertices deleted or replaced on ' + obj.name)
        # Verify original triangle connectivity and section membership exactly.
        expected_faces = {}
        for si, sec in enumerate(block['sections']):
            for tri in import_lod0.faces_from_strip(sec['indices'], restart):
                key = (si, tuple(sorted(tri)))
                expected_faces[key] = expected_faces.get(key, 0) + 1
        actual_faces = {}
        for poly in mesh.polygons:
            if face_ids.data[poly.index].value == 0:
                continue
            si = int(face_ids.data[poly.index].value)-1
            tri = tuple(int(ids.data[vi].value)-1 for vi in poly.vertices)
            if len(tri) != 3 or min(tri) < 0:
                raise ValueError('Original triangle changed on ' + obj.name)
            key = (si, tuple(sorted(tri)))
            actual_faces[key] = actual_faces.get(key, 0) + 1
        if actual_faces != expected_faces:
            raise ValueError('Original source triangles or sections modified on ' + obj.name)
        # Initial native topology support is deliberately restricted to fully
        # new triangles. Mixed old/new faces require explicit source splitting.
        for poly in new_polys:
            if len(poly.vertices) != 3 or any(ids.data[i].value != 0 for i in poly.vertices):
                raise ValueError('Dev15 requires triangles made entirely of new vertices on ' + obj.name)
        if new_verts and not new_polys:
            raise ValueError('Unreferenced new vertices on ' + obj.name)
        if new_polys and not new_verts:
            raise ValueError('New faces without new vertices on ' + obj.name)
        used_new = {i for p in new_polys for i in p.vertices}
        if set(new_verts) != used_new:
            raise ValueError('Loose new vertices on ' + obj.name)
        uv0 = mesh.uv_layers.get('UV0')
        uv1 = mesh.uv_layers.get('UV1')
        if new_polys and (uv0 is None or uv1 is None):
            raise ValueError('New geometry needs UV0 and UV1 on ' + obj.name)
        sections = block['sections']
        section_attr = mesh.attributes.get('fh1_section_index')
        new_by_section = {}
        for poly in new_polys:
            # Blender initializes INT face attributes to zero on newly created
            # polygons. That is NOT a valid provenance assignment: it often
            # points at the body section even when the user selected misc.
            # Material assignment is authoritative for new geometry.
            material_slot = int(poly.material_index)
            if material_slot < 0 or material_slot >= len(mesh.materials):
                raise ValueError('Invalid material slot on new face %d of %s' % (poly.index, obj.name))
            material = mesh.materials[material_slot]
            # Imported Blender materials are named FH1_<shader>, while native
            # sections store the bare shader name (e.g. misc). Prefer the
            # importer-provided shader identity over the display name.
            shader_name = material.get('fh1_shader_name')
            if not shader_name:
                shader_name = material.name[4:] if material.name.startswith('FH1_') else material.name
            # Dev98: for newly assigned native shaders, encode geometry using
            # a donor section without changing the material's shader identity.
            # The experimental section writer then moves only these faces.
            custom_shader = str(material.get('fh1_custom_native_shader', '')).strip()
            if custom_shader and not any(sec['name'].lower() == custom_shader.lower() for sec in sections):
                from .custom_shader_export import _donor
                shader_name = sections[_donor(sections)]['name']
            candidates = [i for i, sec in enumerate(sections) if
                          sec['name'] == shader_name]
            if not candidates:
                raise ValueError('New face %d on %s uses material %s, which has no original CARBIN section' %
                                 (poly.index, obj.name, material.name))
            if len(candidates) == 1:
                section_id = candidates[0]
            else:
                # For repeated material sections, use explicit section index
                # only if it belongs to the selected material. Otherwise do
                # not silently assign the plate to an arbitrary section.
                proposed = int(section_attr.data[poly.index].value) if section_attr else -1
                if proposed in candidates:
                    section_id = proposed
                else:
                    raise ValueError('Material %s on %s face %d has multiple native sections %s; assign fh1_section_index to one of them' %
                                     (material.name, obj.name, poly.index, candidates))
            new_by_section.setdefault(section_id, []).append(poly)

        # Dev26: resolve UVs once, before atlas expansion and native vertex encoding.
        # Blender UVs are per-loop. A single malformed corner must not poison the
        # section atlas; only repair when other corners of THE SAME vertex in
        # THE SAME section unanimously establish its intended UV0.
        resolved_uv0 = {}
        resolved_uv1 = {}
        loops_by_vertex_section = {}
        for section_id, polys in new_by_section.items():
            for poly in polys:
                for li in poly.loop_indices:
                    vi = mesh.loops[li].vertex_index
                    loops_by_vertex_section.setdefault((section_id, vi), []).append(li)
        def sane_uv(pair):
            return all(math.isfinite(float(x)) and abs(float(x)) <= 1e6 for x in pair)
        for (section_id, vi), loop_indices in loops_by_vertex_section.items():
            sec = sections[section_id]
            for channel, layer, output, key in (
                (0, uv0, resolved_uv0, 'atlas'),
                (1, uv1, resolved_uv1, 'atlas1')):
                atlas = sec[key]
                def in_atlas(pair):
                    if not sane_uv(pair):
                        return False
                    absolute = (float(pair[0]), 1.0-float(pair[1]))
                    for axis in range(2):
                        offset, scale = atlas[2*axis:2*axis+2]
                        if abs(scale) < 1e-12:
                            if abs(absolute[axis]-offset) > 2e-5:
                                return False
                        elif not -1e-5 <= (absolute[axis]-offset)/scale <= 1.00001:
                            return False
                    return True
                samples = {li: tuple(float(x) for x in layer.data[li].uv) for li in loop_indices}
                good = [pair for pair in samples.values() if in_atlas(pair)]
                for li, pair in samples.items():
                    if sane_uv(pair) and (in_atlas(pair) or not good):
                        output[li] = pair
                        continue
                    # Only use a consensus from the same native vertex and section.
                    # Never copy UV1 to UV0 or infer from a different vertex.
                    unique = []
                    for candidate in good:
                        if not any(max(abs(candidate[k]-v[k]) for k in range(2)) < 1e-6 for v in unique):
                            unique.append(candidate)
                    if len(unique) == 1:
                        output[li] = unique[0]
                        print('[FH1 Dev26 UV REPAIR] %s vertex %d loop %d section %s UV%d: %r -> %r (same-vertex loop consensus)' %
                              (obj.name, vi, li, sec['name'], channel, pair, unique[0]))
                    else:
                        output[li] = pair
                        if not sane_uv(pair):
                            raise ValueError('Dev26: invalid UV%d on %s vertex %d loop %d section %s: %r; no unambiguous same-vertex correction' %
                                             (channel,obj.name,vi,li,sec['name'],pair))
        # The resolved snapshot is reused in both atlas construction and encoding.
        raw = block['vertices']
        n = block['count']
        vals = list(obj['fh1_position_header_values'])
        trans, dmin, dmax = vals[:3], vals[3:6], vals[6:9]
        low = [min(v[k] for v in raw) for k in range(3)]
        high = [max(v[k] for v in raw) for k in range(3)]
        if any(high[k] - low[k] < 1e-12 or dmax[k] - dmin[k] < 1e-12 for k in range(3)):
            raise ValueError('Degenerate native bounds on ' + obj.name)
        # Preserve unused source vertices, including their exact original positions.
        wanted = [tuple(trans[k] + dmin[k] + (raw[i][k]-low[k]) /
                        (high[k]-low[k])*(dmax[k]-dmin[k]) for k in range(3)) for i in range(n)]
        seen = {}
        changed = False
        for v in mesh.vertices:
            # Source ID zero is the importer's sentinel for newly attached
            # vertices. They are encoded and appended below, not looked up
            # in the original native vertex buffer.
            if ids.data[v.index].value == 0:
                continue
            source_id = ids.data[v.index].value - 1
            if not 0 <= source_id < n:
                raise ValueError('Invalid source vertex ID %d on %s vertex %d' %
                                 (source_id + 1, obj.name, v.index))
            local = inv @ (obj.matrix_world @ v.co)
            target = tuple(float(local[k]) for k in range(3))
            if not all(math.isfinite(x) for x in target):
                raise ValueError('Non-finite vertex on ' + obj.name)
            if source_id in seen and max(abs(target[k]-seen[source_id][k]) for k in range(3)) > 1e-6:
                raise ValueError('Conflicting edits to shared source vertex %d on %s' % (source_id, obj.name))
            seen[source_id] = target
            if max(abs(target[k]-wanted[source_id][k]) for k in range(3)) > 1e-5:
                changed = True
            wanted[source_id] = target
        if not changed and not new_verts:
            continue
        new_vertex_ids = {vid: n + i for i, vid in enumerate(new_verts)}
        for vid in new_verts:
            local = inv @ (obj.matrix_world @ mesh.vertices[vid].co)
            wanted.append(tuple(float(local[k]) for k in range(3)))
        n_total = len(wanted)
        # IN-GAME SAFETY EXPERIMENT: keep the original component's position
        # header and every original XYZ/W record byte-for-byte unchanged.
        # The old writer normalized the entire trunk to [-1,1] and rewrote
        # its bounds. Forza Studio tolerated that; the game did not.
        # This experiment supports additions ONLY inside the original packed
        # coordinate extent; edits to original positions are intentionally
        # rejected rather than silently breaking the whole component.
        if changed:
            raise ValueError('In-game safe mode: existing vertex position edits are disabled on ' + obj.name)
        old = [struct.unpack_from('>4h', original, block['offset']+20+i*28+4) for i in range(n)]
        denom = 32767.0**2
        packed = [list(v[:3]) for v in old]
        packed_w = [v[3] for v in old]
        for i in range(n, n_total):
            coords = []
            for k in range(3):
                # The importer maps raw extrema to native position bounds.
                # Do not extrapolate: a new vertex outside the original
                # extent would need a header rewrite, currently unsafe.
                normalized = (wanted[i][k]-trans[k]-dmin[k])/(dmax[k]-dmin[k])
                if normalized < -1e-5 or normalized > 1.00001:
                    raise ValueError('In-game safe mode: new vertex %d of %s lies outside original axis %s bounds (%.5f); no file written' % (i,obj.name,'XYZ'[k],normalized))
                raw_target = low[k]+normalized*(high[k]-low[k])
                value = round(raw_target*32767)
                if not -32768 <= value <= 32767:
                    raise ValueError('In-game safe mode: new vertex coordinate overflow on '+obj.name)
                coords.append(value)
            packed.append(coords)
            packed_w.append(32767)
        # Validate new positions against the unchanged original header/extrema.
        for i in range(n, n_total):
            for k in range(3):
                rawval = packed[i][k]*packed_w[i]/denom
                actual = trans[k]+dmin[k]+(rawval-low[k])/(high[k]-low[k])*(dmax[k]-dmin[k])
                worst_error = max(worst_error,abs(actual-wanted[i][k]))
        if worst_error > 0.002:
            raise ValueError('In-game safe mode: quantization error %.6f on %s; no file written' % (worst_error,obj.name))
        # Crucially: NO changes to position header or original vertex records.
        if new_verts:
            # Rebuild UV0 and UV1 atlas transforms when new attached geometry
            # falls outside the existing section range. Both channels use the
            # same per-section affine mapping and unsigned 16-bit UV storage.
            # Re-encode source records while checking shared-section conflicts.
            uv_rewrites = [{}, {}]
            original_atlases = [{id(sec): tuple(sec[key]) for sec in sections}
                                for key in ('atlas', 'atlas1')]
            changed_sections = [set(), set()]
            for channel, (key, layer, source_uv, header_delta) in enumerate((
                ('atlas', uv0, block['uv0'], 68),
                ('atlas1', uv1, block['uv1'], 52),
            )):
                for sec_id, polys in new_by_section.items():
                    sec = sections[sec_id]
                    old_atlas = tuple(sec[key])
                    target = [(float((resolved_uv0 if channel == 0 else resolved_uv1)[li][0]),
                               1.0-float((resolved_uv0 if channel == 0 else resolved_uv1)[li][1]))
                              for poly in polys for li in poly.loop_indices]
                    indices = {idx for idx in sec['indices'] if idx < restart}
                    if not indices:
                        raise ValueError('No original section vertices for UV remapping on ' + obj.name)
                    source = [(old_atlas[0] + source_uv[i][0]*old_atlas[1],
                               old_atlas[2] + source_uv[i][1]*old_atlas[3])
                              for i in indices]
                    new_atlas = list(old_atlas)
                    for axis in range(2):
                        offset, scale = 2*axis, 2*axis+1
                        lo = min(v[axis] for v in source + target)
                        hi = max(v[axis] for v in source + target)
                        old_lo = min(old_atlas[offset], old_atlas[offset]+old_atlas[scale])
                        old_hi = max(old_atlas[offset], old_atlas[offset]+old_atlas[scale])
                        if lo >= old_lo-1e-6 and hi <= old_hi+1e-6 and abs(old_atlas[scale]) > 1e-12:
                            continue
                        # Expand only when needed. Preserve atlas direction.
                        lo = min(lo, old_lo)
                        hi = max(hi, old_hi)
                        # Allow a tiny safety margin for the native float32 atlas
                        # header and Blender float32 UV loop values. A range
                        # ending exactly at a target UV can otherwise round
                        # inward when serialized, failing on a fresh export.
                        margin = max(1e-6, (hi-lo)*2e-6)
                        lo -= margin
                        hi += margin
                        if hi-lo < 1e-10:
                            lo -= 0.5
                            hi += 0.5
                        if old_atlas[scale] < 0:
                            new_atlas[offset], new_atlas[scale] = hi, lo-hi
                        else:
                            new_atlas[offset], new_atlas[scale] = lo, hi-lo
                    if tuple(new_atlas) == old_atlas:
                        continue
                    atlas_at = sec['section_name_offset'] - header_delta
                    if atlas_at < 0 or atlas_at+16 > len(original):
                        raise ValueError('Invalid UV%d atlas header on %s' % (channel,obj.name))
                    if struct.unpack_from('>4f', original, atlas_at) != old_atlas:
                        raise ValueError('UV%d atlas header mismatch on %s' % (channel,obj.name))
                    replacements.extend((atlas_at+4*k, '>f', val) for k,val in enumerate(new_atlas))
                    for i in indices:
                        uv = source_uv[i]
                        absolute = (old_atlas[0]+uv[0]*old_atlas[1],
                                    old_atlas[2]+uv[1]*old_atlas[3])
                        normalized = [(absolute[k]-new_atlas[2*k])/new_atlas[2*k+1] for k in range(2)]
                        if any(v < -1e-6 or v > 1.000001 for v in normalized):
                            raise ValueError('UV%d repack overflow on %s vertex %d' % (channel,obj.name,i))
                        packed_uv = tuple(max(0,min(65535,round(v*65535))) for v in normalized)
                        if i in uv_rewrites[channel] and uv_rewrites[channel][i] != packed_uv:
                            raise ValueError('UV%d shared vertex conflict on %s vertex %d' % (channel,obj.name,i))
                        uv_rewrites[channel][i] = packed_uv
                    sec[key] = tuple(new_atlas)
                    changed_sections[channel].add(sec_id)
                for sec_id, sec in enumerate(sections):
                    for i in set(sec['indices']):
                        if i not in uv_rewrites[channel]:
                            continue
                        before = original_atlases[channel][id(sec)]
                        old_uv = tuple(before[2*k]+source_uv[i][k]*before[2*k+1] for k in range(2))
                        packed_uv_pair = uv_rewrites[channel][i]
                        after = tuple(sec[key][2*k]+packed_uv_pair[k]/65535*sec[key][2*k+1] for k in range(2))
                        if max(abs(old_uv[k]-after[k]) for k in range(2)) > 2e-5:
                            raise ValueError('UV%d atlas change conflicts with section %d on %s vertex %d' % (channel,sec_id,obj.name,i))
                for i, pair in uv_rewrites[channel].items():
                    struct.pack_into('>2H', out, block['offset']+20+i*28+12+4*channel, *pair)
            # A source record is 4-byte opaque prefix, XYZ/W shorts, UV0/UV1
            # unsigned shorts and a quaternion-like 4-short normal payload.
            # Reuse the native record prefix and normal payload from an existing
            # vertex; normal encoding is derived from the new face normal.
            # A CARBIN record has one normal and UV pair. Blender stores both per
            # face corner, so create additional native records for discontinuities.
            mesh.calc_normals_split()
            corner_records = {}
            vertex_variants = {}
            vertex_uv = {}
            vertex_normal = {}
            split_position_indices = {}  # Native duplicate -> original new-vertex position index
            for sec_id, polys in new_by_section.items():
                sec = sections[sec_id]
                for poly in polys:
                    for li in poly.loop_indices:
                        vi = mesh.loops[li].vertex_index
                        u, v = resolved_uv0[li]
                        u1, v1 = resolved_uv1[li]
                        def unatlas(uv, atlas):
                            a, b, c, d = atlas
                            def decode_axis(value, offset, scale, axis):
                                if abs(scale) < 1e-12:
                                    if abs(value-offset) > 2e-5:
                                        raise ValueError('New UV cannot be represented by constant %s atlas axis on %s' % (axis,obj.name))
                                    return 0.0
                                return (value-offset)/scale
                            return (decode_axis(float(uv[0]),a,b,'U'),
                                    decode_axis(1.0-float(uv[1]),c,d,'V'))
                        pair = unatlas((u,v),sec['atlas']) + unatlas((u1,v1),sec['atlas1'])
                        if not all(-1e-6 <= x <= 1.000001 for x in pair):
                            raise ValueError(
                                'New UV outside source atlas on %s vertex %d, section %d (%s), '
                                'UV0=%r UV1=%r, atlas0=%r atlas1=%r, decoded=%r. '
                                'Please send this full message for diagnosis.' %
                                (obj.name, vi, sec_id, sec['name'], (float(u),float(v)),
                                 (float(u1),float(v1)), sec['atlas'], sec['atlas1'], pair))
                        normal = tuple((inv @ mesh.loops[li].normal).normalized())
                        variants = vertex_variants.setdefault(vi,[])
                        idx = next((entry[0] for entry in variants if
                                    max(abs(pair[k]-entry[1][k]) for k in range(4)) <= 1e-5 and
                                    sum((normal[k]-entry[2][k])**2 for k in range(3)) <= 1e-8),None)
                        if idx is None:
                            if not variants:
                                idx = new_vertex_ids[vi]
                            else:
                                idx = len(packed)
                                packed.append(list(packed[new_vertex_ids[vi]]))
                                packed_w.append(packed_w[new_vertex_ids[vi]])
                                split_position_indices[idx] = new_vertex_ids[vi]
                            variants.append((idx,pair,normal))
                            vertex_uv[idx] = pair
                            vertex_normal[idx] = normal
                        corner_records[li] = idx
            n_total = len(packed)
            if n_total >= restart:
                raise ValueError('Native index width exhausted by split normals on '+obj.name)
            new_records = bytearray()
            for idx in range(n, n_total):
                normal = Vector(vertex_normal[idx]).normalized()
                # Native packed normal is quaternion XYZW rotating +X to normal.
                q = Vector((1.0, 0.0, 0.0)).rotation_difference(normal)
                quat = (q.x, q.y, q.z, q.w)
                decoded = (1-2*(q.y*q.y+q.z*q.z), 2*(q.x*q.y+q.z*q.w), 2*(q.x*q.z-q.y*q.w))
                if sum(decoded[k]*normal[k] for k in range(3)) < 0.9999:
                    raise ValueError("Custom normal round-trip failed on %s vertex %d" % (obj.name, vid))
                record = bytearray(original[block['offset']+20:block['offset']+48])
                struct.pack_into('>4h', record, 4, *(packed[idx] + [packed_w[idx]]))
                struct.pack_into('>4H', record, 12, *(max(0,min(65535,round(x*65535))) for x in vertex_uv[idx]))
                struct.pack_into('>4h', record, 20, *(max(-32767,min(32767,round(x*32767))) for x in quat))
                new_records.extend(record)
            # Each new triangle is a fresh strip separated by a restart index.
            # Insert before the section's original index payload end, keeping
            # all original indices and their triangle winding unchanged.
            for sec_id, polys in new_by_section.items():
                sec = sections[sec_id]
                indices = []
                for poly in polys:
                    indices.extend([restart] + [corner_records[li] for li in poly.loop_indices])
                if n_total >= restart:
                    raise ValueError('Native index width exhausted on ' + obj.name)
                payload = struct.pack(('>%dH' if index_width == 2 else '>%dI') % len(indices), *indices)
                insertions.append((sec['end'], payload))
                replacements.append((sec['section_header_offset']+72, '>I', len(sec['indices'])+len(indices)))
                added_faces += len(polys)
            # Dev17 diagnostic: the native 28-byte vertex stream is followed
            # by a second 4-byte-per-vertex stream. Keep their counts aligned.
            # The meaning of this stream is NOT yet established; neutral zero
            # entries are a controlled in-game experiment, not a damage fix.
            secondary_header = block['offset'] + 20 + n * 28
            secondary_count_at = secondary_header + 29
            secondary_data_at = secondary_header + 41
            if secondary_data_at + n * 4 > len(original):
                raise ValueError('Secondary vertex stream truncated on ' + obj.name)
            secondary_count = struct.unpack_from('>I', original, secondary_count_at)[0]
            secondary_stride = struct.unpack_from('>I', original, secondary_header + 33)[0]
            secondary_flag = struct.unpack_from('>I', original, secondary_header + 37)[0]
            secondary_type = struct.unpack_from('>I', original, secondary_header + 25)[0]
            if (secondary_count, secondary_stride, secondary_flag, secondary_type) != (n, 4, 1, 3):
                raise ValueError('Dev17: cannot safely extend unknown secondary stream on %s: count=%d stride=%d flag=%d type=%d' % (obj.name, secondary_count, secondary_stride, secondary_flag, secondary_type))
            replacements.append((secondary_count_at, '>I', n_total))
            # DEV17 EXACT DEFAULT: neutral four-byte records for each appended vertex.
            # The experimental donor option must never affect this default.
            secondary_payload = bytes(4 * (n_total - n))
            if damage_donor.enabled() and n_total > n:
                original_records = [original[secondary_data_at+i*4:secondary_data_at+(i+1)*4] for i in range(n)]
                donor_positions = wanted[:n]
                if damage_donor.donor_mode() not in ('SAME', 'OFF'):
                    donor_key = damage_donor.donor_mode()
                    donor_offset = int(donor_key[2:]) if donor_key.startswith('N:') else None
                    donor = next((b for b in blocks if b['offset'] == donor_offset), None)
                    if donor is None:
                        raise ValueError('Selected deformation donor not found in source CARBIN: ' + donor_key)
                    donor_obj = next(o for o in objects if source_header_offset(o) == donor['offset'])
                    donor_vals = list(donor_obj['fh1_position_header_values'])
                    dt, dmin2, dmax2 = donor_vals[:3], donor_vals[3:6], donor_vals[6:9]
                    dr = donor['vertices']
                    lo2 = [min(v[k] for v in dr) for k in range(3)]
                    hi2 = [max(v[k] for v in dr) for k in range(3)]
                    if any(hi2[k]-lo2[k] < 1e-12 for k in range(3)):
                        raise ValueError('Degenerate deformation donor bounds')
                    donor_positions = [tuple(dt[k]+dmin2[k]+(v[k]-lo2[k])/(hi2[k]-lo2[k])*(dmax2[k]-dmin2[k]) for k in range(3)) for v in dr]
                    dh = donor['offset'] + 20 + donor['count']*28
                    dc = struct.unpack_from('>I', original, dh+29)[0]
                    ds = struct.unpack_from('>I', original, dh+33)[0]
                    df = struct.unpack_from('>I', original, dh+37)[0]
                    dk = struct.unpack_from('>I', original, dh+25)[0]
                    if (dc,ds,df,dk) != (donor['count'],4,1,3):
                        raise ValueError('Selected donor secondary stream layout mismatch')
                    donor_start = dh+41
                    original_records = [original[donor_start+i*4:donor_start+(i+1)*4] for i in range(dc)]
                    print('[FH1 Dev32] Native selected donor:', donor['label'], 'for', block['label'])
                # wanted contains original plus one position per Blender vertex,
                # while packed can grow further with face-corner normal/UV splits.
                # Every appended native vertex MUST have a corresponding donor
                # record, including these split variants.
                targets = [wanted[split_position_indices.get(i, i)]
                           for i in range(n, n_total)]
                if len(donor_positions) != len(original_records):
                    raise ValueError('Dev18 LOD0 donor positions/records mismatch: %d vs %d' % (len(donor_positions), len(original_records)))
                if targets:
                    sample = []
                    distances = []
                    for i, pt in enumerate(targets):
                        idx = min(range(len(donor_positions)), key=lambda j: sum((float(pt[k])-float(donor_positions[j][k]))**2 for k in range(3)))
                        dist = math.sqrt(sum((float(pt[k])-float(donor_positions[idx][k]))**2 for k in range(3)))
                        distances.append(dist)
                        if i < 8:
                            sample.append((i, idx, round(dist, 6), original_records[idx].hex()))
                    print('[FH1 Dev18 AUDIT] LOD0 target=%s donor=%s vertices=%d distances min/mean/max=%.6f/%.6f/%.6f sample (target, donor, distance, bytes)=%s' % (obj.name, donor['label'] if damage_donor.donor_mode() not in ('SAME', 'OFF') else obj.name, len(targets), min(distances), sum(distances)/len(distances), max(distances), sample))
                secondary_payload = damage_donor.nearest_records(donor_positions, original_records, targets)
                print('[FH1 Dev21] Native nearest-source secondary records copied:', obj.name, n_total-n,
                      'including', len(split_position_indices), 'split vertices')
            # Dev25: deliberately speculative four-byte record experiment.
            # FH1's actual secondary-record semantics remain unknown.
            # Keep this opt-in and never modify original secondary records.
            addon = bpy.context.preferences.addons.get(__package__)
            prefs = addon.preferences if addon else None
            if prefs and getattr(prefs, 'shape_record_experiment', False) and n_total > n:
                shape_name = str(getattr(obj, 'fh1_damage_shape_ui', '') or '')
                keys = mesh.shape_keys
                if not keys or not shape_name or shape_name not in keys.key_blocks or keys.key_blocks[shape_name] == keys.reference_key:
                    raise ValueError('Dev25: select a non-Basis Damage Shape Key for %s in Object Data > FH1 Damage Research' % obj.name)
                target_key = keys.key_blocks[shape_name]
                basis_key = keys.reference_key
                if len(target_key.data) != len(mesh.vertices):
                    raise ValueError('Dev25 shape-key vertex count mismatch on ' + obj.name)
                native_to_blender = {native: blender for blender, native in new_vertex_ids.items()}
                limit = {'002': 0.02, '010': 0.10, '050': 0.50}[prefs.shape_record_range]
                encoded = bytearray()
                changed_count = 0
                for native_idx in range(n, n_total):
                    source_native_idx = split_position_indices.get(native_idx, native_idx)
                    vi = native_to_blender.get(source_native_idx)
                    if vi is None:
                        raise ValueError('Dev25: native split vertex has no Blender shape-key source')
                    local_delta = target_key.data[vi].co - basis_key.data[vi].co
                    # Transform as a direction (no translation); use same axis
                    # conversion as primary native position encoding.
                    delta = inv @ (obj.matrix_world.to_3x3() @ local_delta)
                    q = [max(-127, min(127, round(float(delta[k]) / limit * 127))) for k in range(3)]
                    if any(q):
                        changed_count += 1
                    # Experimental layout: signed X,Y,Z, reserved zero byte.
                    # This is a test encoding, NOT a confirmed FH1 format.
                    encoded.extend(struct.pack('>3bB', *q, 0))
                secondary_payload = bytes(encoded)
                print('[FH1 Dev25 EXPERIMENT] %s shape=%s native_records=%d nonzero=%d range=%.3fm first_records=%s' %
                      (obj.name, shape_name, n_total-n, changed_count, limit,
                       [secondary_payload[i:i+4].hex() for i in range(0, min(32,len(secondary_payload)),4)]))
            expected_secondary_bytes = 4 * (n_total - n)
            if len(secondary_payload) != expected_secondary_bytes:
                raise ValueError('Dev21 LOD0 secondary stream length mismatch on %s: got %d, expected %d (%d native new vertices, %d split)' %
                                 (obj.name, len(secondary_payload), expected_secondary_bytes,
                                  n_total-n, len(split_position_indices)))
            insertions.append((secondary_data_at + n * 4, secondary_payload))
            replacements.append((block['offset']+8, '>I', n_total))
            insertions.append((block['offset']+20+n*28, bytes(new_records)))
            added_vertices += n_total - n
    # Dev19 diagnostic: ensure all planned insertions are inside the original
    # file and report the complete LOD0 write plan before changing any bytes.
    for at, payload in insertions:
        if not (0 <= at <= len(original)):
            raise ValueError('Dev20: insertion outside source at %d (%d bytes)' % (at, len(payload)))
    for at, fmt, value in replacements:
        if not (0 <= at and at + struct.calcsize(fmt) <= len(original)):
            raise ValueError('Dev20: replacement outside source at %d (%s)' % (at, fmt))
    print('[FH1 Dev20 LOD0] donor_enabled=%s original_size=%d added_vertices=%d insertions=%s replacements=%d' %
          (damage_donor.enabled(), len(original), added_vertices,
           [(at, len(payload)) for at, payload in sorted(insertions)], len(replacements)))
    # Apply original-coordinate replacements and insertions from right to left.
    # At identical offsets, insertions must precede replacement operations.
    for at, fmt, value in replacements:
        struct.pack_into(fmt, out, at, value)
    for at, payload in sorted(insertions, key=lambda x: x[0], reverse=True):
        out[at:at] = payload
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(out)
    return added_vertices, added_faces, worst_error, len(out), edited
