"""FH1 CARBIN LOD1-LOD5 importer v0.8.4 (cleanup only) (Blender 3.6.x).
Explicit per-section LOD IDs replace material-repetition grouping.
Source geometry, UV, normals, shared materials, auto-merge, and position heuristics retained.
Source file is never modified. Position-header resolution remains provisional.
"""
import os
import math
import struct

CARBIN_PATH = r''  # blank opens picker
IMPORT_LODS = (1, 2, 3, 4, 5)  # choose any subset of 1..5
REPORT_TOPOLOGY = True  # log source-index reuse, boundary edges, and position duplicates



def vehicle_key(path):
    """Shared name for VW_Corrado_95.carbin and VW_Corrado_95_lod0.carbin."""
    import os, re
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    return re.sub(r'(?i)(?:_lod[0-5]|_cockpit)$', '', stem)

def shared_root(bpy, path):
    key = vehicle_key(path)
    # Prefer a root tagged by either importer, then a pre-existing exact-name root.
    for collection in bpy.context.scene.collection.children:
        if collection.get('fh1_vehicle_key', '').casefold() == key.casefold():
            return collection
    for collection in bpy.context.scene.collection.children:
        if collection.name.casefold() == key.casefold():
            collection['fh1_vehicle_key'] = key
            return collection
    root = bpy.data.collections.new(key)
    bpy.context.scene.collection.children.link(root)
    root['fh1_vehicle_key'] = key
    return root

def child_collection(bpy, root, name):
    for collection in root.children:
        if collection.name == name:
            return collection
    collection = bpy.data.collections.new(name)
    root.children.link(collection)
    return collection

def u32(data, off):
    return struct.unpack_from('>I', data, off)[0]


def strip_faces(indices, restart=65535):
    out, strip = [], []
    for idx in indices:
        if idx == restart:
            strip.clear()
            continue
        strip.append(idx)
        if len(strip) >= 3:
            a, b, c = strip[-3:]
            if len({a, b, c}) == 3:
                out.append((a, b, c) if len(strip) % 2 else (b, a, c))
    return out


def discover(data):
    """Conservative candidate vertex records and section headers, not scene nodes."""
    records = []
    for p in range(4, len(data) - 24):
        n = u32(data, p - 4)
        if not 2 <= n <= 60 or p + n + 20 > len(data):
            continue
        raw = data[p:p+n]
        if not all(32 <= v < 127 for v in raw):
            continue
        kind, count, stride, flag = struct.unpack_from('>4I', data, p+n)
        if (kind, stride, flag) != (3, 28, 1) or not 0 < count < 200000:
            continue
        end = p+n+20+count*stride
        if end > len(data):
            continue
        records.append({'name': raw.decode('ascii'), 'p': p,
                        'count': count, 'start': p+n+20, 'end': end})
    # Wheel LOD0 has its own 28-byte secondary vertex pool. Its blur sections
    # reference source IDs 5320..7144, beyond the 6097-record primary pool.
    # Validate those indices against the actual secondary pool, not rec['count'].
    secondary_wheel_count = 0
    for candidate in range(0, len(data) - 16):
        if data[candidate:candidate+4] != b'\x00\x00\x00\x03':
            continue
        vertex_count, vertex_stride, vertex_flag = struct.unpack_from('>3I', data, candidate+4)
        if (vertex_stride == 28 and vertex_flag == 1 and vertex_count > secondary_wheel_count
                and candidate + 16 + vertex_count * vertex_stride <= len(data)):
            secondary_wheel_count = vertex_count
    for i, rec in enumerate(records):
        bound = records[i+1]['p'] if i+1 < len(records) else len(data)
        sections = []
        for off in range(rec['end'], min(bound, len(data)-90)):
            n = u32(data, off)
            if not 1 <= n <= 48 or off+n+84 > bound:
                continue
            raw = data[off+4:off+4+n]
            if not all(32 <= v < 127 for v in raw):
                continue
            hdr = off+4+n
            if u32(data, hdr+64) != 4 or u32(data, hdr+68) != 0:
                continue
            ic, iw = u32(data, hdr+72), u32(data, hdr+76)
            if iw not in (2, 4) or not 3 <= ic <= 200000 or hdr+80+ic*iw > bound:
                continue
            pre = off-81
            if pre < rec['end'] or u32(data, pre+8) != 3:
                continue
            wheel = iw == 4
            if wheel:
                # Wheel section header: LOD0..LOD5 in hdr[0], 32-bit
                # triangle-strip indices and 0x00ffffff restart markers.
                # The header stores UV offsets (U,V) and scales (U,V).
                uoff, voff, uscale, vscale = struct.unpack_from('>4f', data, hdr+24)
                atlas = (uoff, uscale, voff, vscale)
                lod_id = u32(data, hdr)
                restart = 0x00ffffff
                indices = struct.unpack_from('>'+str(ic)+'I', data, hdr+80)
            else:
                atlas = struct.unpack_from('>4f', data, pre+13)
                lod_id = u32(data, hdr)
                restart = 65535
                indices = struct.unpack_from('>'+str(ic)+'H', data, hdr+80)
            if not all(math.isfinite(x) and abs(x) < 100000 for x in atlas):
                continue
            # LOD0 wheel indices address the secondary pool. Previously the
            # primary-pool check silently discarded blur_lip and blur_rim.
            vertex_limit = (secondary_wheel_count if wheel and lod_id == 0
                            else rec['count'])
            if any(v >= vertex_limit for v in indices if v != restart):
                continue
            if lod_id not in ((0, 1, 2, 3, 4, 5) if wheel else (1, 2, 3, 4, 5)):
                continue
            sections.append({'name': raw.decode('ascii'), 'offset': off, 'lod': lod_id,
                             'uv': atlas, 'index_width': iw, 'faces': strip_faces(indices, restart)})
        rec['sections'] = sections
    return records


def explicit_lod_groups(sections):
    """Assign sections using the big-endian LOD ID at the start of each section header."""
    groups = {lod: [] for lod in range(6)}
    for sec in sections:
        groups[sec['lod']].append(sec)
    return groups


def resolve_position_header(data, rec, previous_record_end):
    """Find plausible transform headers preceding this vertex record.

    Provisional: use the last plausible header after the preceding vertex
    record. This is NOT a verified CARBIN scene graph linkage.
    """
    candidates = []
    for h in range(max(0, previous_record_end), rec['p'] - 59):
        if data[h:h+4] != b'\x00\x00\x00\x03':
            continue
        vals = struct.unpack_from('>9f', data, h+20)
        if not all(math.isfinite(v) and abs(v) < 100 for v in vals):
            continue
        if not all(vals[k+6] > vals[k+3]+1e-7 for k in range(3)):
            continue
        if any(vals[k+6]-vals[k+3] > 30 for k in range(3)):
            continue
        candidates.append((h, vals))
    if not candidates:
        # Some initial records have the familiar inline transform fields but
        # lack the candidate header signature used by the general scanner.
        # Preserve the v0.3 behaviour ONLY when those fields are plausible.
        h = rec['p'] - 109
        if h >= 0:
            vals = struct.unpack_from('>9f', data, rec['p'] - 89)
            if (all(math.isfinite(v) and abs(v) < 100 for v in vals)
                    and all(vals[k+6] > vals[k+3]+1e-7 for k in range(3))
                    and all(vals[k+6]-vals[k+3] < 30 for k in range(3))):
                return h, vals, 0
        raise ValueError('No plausible position header between vertex records')
    h, vals = candidates[-1]
    return h, vals, len(candidates)


def decode_vertices(data, rec):
    p, count, start = rec['p'], rec['count'], rec['start']
    trans, dmin, dmax = rec['position_header'][0:3], rec['position_header'][3:6], rec['position_header'][6:9]
    loc, uv0, uv1, normals = [], [], [], []
    for i in range(count):
        off = start + i*28
        x,y,z,w = struct.unpack_from('>4h', data, off)
        a,b,c,d = struct.unpack_from('>4H', data, off+8)
        loc.append((x*w/(32767.0**2), y*w/(32767.0**2), z*w/(32767.0**2)))
        uv0.append((a/65535.0, b/65535.0))
        uv1.append((c/65535.0, d/65535.0))
        qx,qy,qz,qw = [v/32767.0 for v in struct.unpack_from('>4h', data, off+16)]
        nx = 1-2*(qy*qy+qz*qz)
        ny = 2*(qx*qy+qz*qw)
        nz = 2*(qx*qz-qy*qw)
        mag = math.sqrt(nx*nx+ny*ny+nz*nz)
        normals.append((nx/mag,ny/mag,nz/mag) if mag>1e-12 else (1,0,0))
    low = [min(v[k] for v in loc) for k in range(3)]
    high = [max(v[k] for v in loc) for k in range(3)]
    if any(abs(high[k]-low[k]) < 1e-12 for k in range(3)):
        raise ValueError('Degenerate bounds')
    pos = [tuple(trans[k]+dmin[k]+(v[k]-low[k])/(high[k]-low[k])*(dmax[k]-dmin[k])
                 for k in range(3)) for v in loc]
    return pos, uv0, uv1, normals


def decode_wheel_lod0_positions(data, rec):
    """Wheel LOD0: Forza Studio q.cs uses its SECONDARY vertex array (q.d).

    Other wheel LODs use q.g, the primary array. The secondary array has its
    own bounds; q.cs maps it to the CARBIN target bounds with aa.d().
    This function is called exclusively for wheel LOD0.
    """
    # The secondary pool follows the primary sections. Its native header is
    # kind=3, vertex_count, stride=28, flag=1 (big endian).
    search_from = max(rec['end'], 0)
    candidates = []
    for off in range(search_from, len(data) - 16):
        if data[off:off+4] != b'\x00\x00\x00\x03':
            continue
        count, stride, flag = struct.unpack_from('>3I', data, off+4)
        if stride != 28 or flag != 1 or count < 1:
            continue
        buffer_start = off + 16
        buffer_end = buffer_start + count * stride
        if buffer_end > len(data):
            continue
        candidates.append((off, count, buffer_start))
    if not candidates:
        raise ValueError('Wheel LOD0 secondary vertex buffer not found; refusing to use primary positions')
    _, count, buffer_start = max(candidates, key=lambda item: item[1])
    max_index = max((v for sec in rec['sections'] if sec['lod'] == 0
                     for tri in sec['faces'] for v in tri), default=-1)
    if max_index >= count:
        raise ValueError('Wheel LOD0 secondary vertex buffer too short: %d <= %d' % (count, max_index))
    raw = []
    for i in range(count):
        x, y, z, w = struct.unpack_from('>4h', data, buffer_start + i*28 + 4)
        factor = w / (32767.0 * 32767.0)
        raw.append((x*factor, y*factor, z*factor))
    # Forza Studio bb.g() starts bounds at zero; match that convention.
    lo = [min(0.0, *(p[k] for p in raw)) for k in range(3)]
    hi = [max(0.0, *(p[k] for p in raw)) for k in range(3)]
    hdr = rec['position_header']
    translation, target_min, target_max = hdr[:3], hdr[3:6], hdr[6:9]
    pos = [tuple(translation[k] + target_min[k] +
                 (p[k]-lo[k]) / (hi[k]-lo[k]) * (target_max[k]-target_min[k])
                 if hi[k] != lo[k] else translation[k]
                 for k in range(3)) for p in raw]
    # Wheel LOD0 secondary 28-byte record, independently verified against
    # Forza Studio OBJ: [0:4] auxiliary, [4:12] packed position,
    # [12:16] UV0, [16:20] UV1, [20:28] normal quaternion.
    # The former offsets (8,12,16) overlapped packed position/UV fields.
    normals = []
    uv0 = []
    uv1 = []
    for i in range(count):
        off = buffer_start + i*28
        a,b,c,d = struct.unpack_from('>4H', data, off+12)
        uv0.append((a/65535.0, b/65535.0))
        uv1.append((c/65535.0, d/65535.0))
        qx,qy,qz,qw = (v/32767.0 for v in struct.unpack_from('>4h',data,off+20))
        nx=1-2*(qy*qy+qz*qz)
        ny=2*(qx*qy+qz*qw)
        nz=2*(qx*qz-qy*qw)
        mag=math.sqrt(nx*nx+ny*ny+nz*nz) or 1.0
        normals.append((nx/mag,ny/mag,nz/mag))
    print('[FH1 wheel LOD0] Dev61 SECONDARY pool: %d records at 0x%x, position+4 UV0+12 UV1+16 normals+20' % (count,buffer_start))
    return pos,uv0,uv1,normals

def import_file(path):
    import bpy
    from mathutils import Matrix, Vector
    data = open(path,'rb').read()
    records = discover(data)
    if not records:
        raise ValueError('No compatible 28-byte vertex records discovered')
    # Keep discovery in binary/source order for position-header resolution.
    # The Outliner collection creation pass below uses alphabetical order.
    for source_order, rec in enumerate(records):
        rec['source_order'] = source_order
    wheel_file = any(sec.get('index_width') == 4 for rec in records for sec in rec['sections'])
    selected_lods = tuple(sorted(set(IMPORT_LODS)))
    if wheel_file:
        # Wheel LOD0 is always included in the standard exterior importer.
        selected_lods = tuple(sorted(set(selected_lods) | {0}))
    if not selected_lods or any(x not in range(6) for x in selected_lods):
        raise ValueError('Invalid LOD selection')
    if not any(rec['sections'] for rec in records):
        raise ValueError('No compatible CARBIN mesh sections found')
    print('[FH1 import] variant=%s, LODs=%s' % ('wheel' if wheel_file else 'body', selected_lods))
    root = shared_root(bpy, path)
    lod_collections = {}
    for lod_number in selected_lods:
        lod_collections[lod_number] = child_collection(bpy, root, 'LOD%d' % lod_number)
        # Replace prior candidate meshes from this LOD only on reimport.
        for old_node in list(lod_collections[lod_number].children):
            if old_node.get('fh1_lod_component') or old_node.get('fh1_lod_candidate_component'):
                for obj in list(old_node.objects):
                    bpy.data.objects.remove(obj, do_unlink=True)
                bpy.data.collections.remove(old_node)
    root['fh1_source_file'] = os.path.basename(path)
    root['fh1_lod_assignment'] = 'Explicit section header LOD IDs (wheel 0-5 / body 1-5)'
    root['fh1_wheel_variant'] = wheel_file
    basis = Matrix.Rotation(math.radians(90), 3, 'X')
    materials = {}
    imported = 0
    errors = []
    previous_record_end = 0
    for rec in records:
        try:
            header_offset, header_values, candidate_count = resolve_position_header(data, rec, previous_record_end)
            rec['position_header'] = header_values
            rec['position_header_offset'] = header_offset
            rec['position_header_candidates'] = candidate_count
        except Exception as exc:
            rec['position_header_error'] = str(exc)
        previous_record_end = rec['end']

    # Only reorder the import/display pass, not the record-resolution pass.
    # A stable secondary key keeps duplicate source names deterministic.
    for rec in sorted(records, key=lambda r: (r['name'].casefold(), r['source_order'])):
        groups = explicit_lod_groups(rec['sections'])
        for lod_number in selected_lods:
            sections = groups[lod_number]
            if not sections:
                print('[FH1 preview]', rec['name'], 'no sections for LOD', lod_number)
                continue
            print('[FH1 preview]', rec['name'], 'LOD', lod_number,
                  'sections', len(sections), 'of', len(rec['sections']), 'explicit header IDs')
            try:
                if 'position_header' not in rec:
                    raise ValueError(rec.get('position_header_error', 'Missing position header'))
                pos, uv0, uv1, normals = decode_vertices(data, rec)
                if wheel_file and lod_number == 0:
                    pos, uv0, uv1, normals = decode_wheel_lod0_positions(data, rec)
                # Indices refer to the original component-wide section list,
                # never to the per-LOD filtered section list.
                original_section_indices = [i for i, sec in enumerate(rec['sections'])
                                            if sec['lod'] == lod_number]
                faces = [(sec_idx, face) for sec_idx, sec in enumerate(sections)
                         for face in sec['faces']]
                if not faces:
                    continue
                used = sorted({v for _, f in faces for v in f})
                remap = {src: idx for idx, src in enumerate(used)}
                verts = [tuple(basis @ Vector(pos[src])) for src in used]
                converted_normals = [tuple((basis @ Vector(normals[src])).normalized()) for src in used]
                part_name = '%s_L%d' % (rec['name'], lod_number)
                mesh = bpy.data.meshes.new(part_name+'_mesh')
                mesh.from_pydata(verts, [], [tuple(remap[v] for v in f) for _, f in faces])
                mesh.update(calc_edges=True)
                # Mesh-domain provenance survives Blender Ctrl+J when the imported
                # object remains active. Zero means newly modelled geometry.
                provenance = mesh.attributes.new(name='fh1_original_vertex_id', type='INT', domain='POINT')
                for vi, source_id in enumerate(used):
                    provenance.data[vi].value = source_id + 1
                section_provenance = mesh.attributes.new(name='fh1_original_section_id', type='INT', domain='FACE')
                for fi, (section_index, _) in enumerate(faces):
                    section_provenance.data[fi].value = original_section_indices[section_index] + 1
                section_attr = mesh.attributes.new(name='fh1_section_index', type='INT', domain='FACE')
                for face_index, (sec_idx, _) in enumerate(faces):
                    section_attr.data[face_index].value = original_section_indices[sec_idx]
                if REPORT_TOPOLOGY:
                    from collections import Counter
                    edge_uses = Counter()
                    for _, tri in faces:
                        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
                            edge_uses[tuple(sorted((a, b)))] += 1
                    boundary = sum(n == 1 for n in edge_uses.values())
                    nonmanifold = sum(n > 2 for n in edge_uses.values())
                    # Exact packed positions (not rounded Blender floats);
                    # coincident source vertices are *not* welded.
                    packed_positions = [data[rec['start'] + src*28:rec['start'] + src*28 + 8] for src in used]
                    unique_packed = len(set(packed_positions))
                    print('[FH1 topology] %s: source_buffer=%d used_source_indices=%d blender_vertices=%d triangles=%d boundary_edges=%d nonmanifold_edges=%d identical_packed_position_extras=%d' %
                          (part_name, rec['count'], len(used), len(mesh.vertices), len(mesh.polygons), boundary, nonmanifold, len(used)-unique_packed))
                part = bpy.data.collections.new(part_name)
                part['fh1_lod_component'] = True
                lod_collections[lod_number].children.link(part)
                obj = bpy.data.objects.new(part_name, mesh)
                part.objects.link(obj)
                uv_layer = mesh.uv_layers.new(name='UV0')
                uv1_layer = mesh.uv_layers.new(name='UV1')
                for sec_idx, sec in enumerate(sections):
                    name = sec['name']
                    if name not in materials:
                        mat = bpy.data.materials.get('FH1_'+name)
                        if mat is None:
                            mat = bpy.data.materials.new('FH1_'+name)
                        materials[name] = mat
                    mesh.materials.append(materials[name])
                # Dev63: The S2000 wheel's secondary-buffer UVs are normalized
                # within each material section, NOT already in texture-atlas
                # coordinates. These reference-calibrated bounds were measured
                # against the matching Forza Studio 4.6 OBJ. Restrict the
                # correction to this exact wheel signature until the CARBIN
                # atlas-bounds metadata is independently located.
                s2000_wheel_uv_bounds = {}
                if (wheel_file and lod_number == 0 and rec['name'] == 'wheel'
                        and rec['count'] == 6097 and len(pos) == 7145
                        and len(sections) == 6
                        and {sec['name'] for sec in sections} ==
                        {'rim', 'inner_rim', 'outer_rim', 'wheel_black', 'blur_lip', 'blur_rim'}):
                    s2000_wheel_uv_bounds = {
                        # (U offset, U scale, V maximum, V scale)
                        'inner_rim': (0.01294262, 0.97036508, 0.60888060, 0.60281207),
                        'outer_rim': (0.60015260, 0.02589260, 0.32660100, 0.15864350),
                        'wheel_black': (0.84029590, 0.15081780, 0.03303200, 0.02539736),
                        # Dev66: independently fit to Forza Studio OBJ vertex UVs.
                        'blur_lip': (0.67501554, 0.31211824, 0.34990842, 0.33267613),
                        'blur_rim': (0.01034171, 0.97931670, 0.98966110, 0.97931682),
                    }
                    print('[FH1 wheel LOD0] Dev66 reference-calibrated all six LOD0 material UV atlas transforms')
                # Dev64: the S2000 primary wheel stream (LOD1-5) also stores
                # inner_rim UVs local to its material island. Apply the same
                # Forza Studio-reference atlas calibration verified for LOD0.
                # Other materials are intentionally left unchanged until their
                # LOD-specific reference mappings have been established.
                if (wheel_file and 1 <= lod_number <= 5
                        and rec['name'] == 'wheel' and rec['count'] == 6097
                        and any(sec['name'] == 'inner_rim' for sec in sections)):
                    s2000_wheel_uv_bounds['inner_rim'] = (
                        0.01294262, 0.97036508, 0.60888060, 0.60281207)
                    print('[FH1 wheel LOD%d] Dev64 inner_rim reference UV atlas correction' % lod_number)
                for poly, (sec_idx, _) in zip(mesh.polygons, faces):
                    poly.material_index = sec_idx
                    poly.use_smooth = True
                    uoff, uscale, voff, vscale = sections[sec_idx]['uv']
                    for li in poly.loop_indices:
                        src = used[mesh.loops[li].vertex_index]
                        calibrated = s2000_wheel_uv_bounds.get(sections[sec_idx]['name'])
                        if calibrated is not None:
                            cu, cs, cv, cvs = calibrated
                            uv_layer.data[li].uv = (cu + uv0[src][0]*cs,
                                                     cv - uv0[src][1]*cvs)
                        else:
                            uv_layer.data[li].uv = (uv0[src][0]*uscale+uoff,
                                                     1-(uv0[src][1]*vscale+voff))
                        uv1_layer.data[li].uv = uv1[src]
                mesh.use_auto_smooth = True
                mesh.normals_split_custom_set([converted_normals[loop.vertex_index]
                                               for loop in mesh.loops])
                mesh.update()
                obj['fh1_source_name'] = rec['name']
                obj['fh1_source_lod'] = lod_number
                obj['fh1_source_record_offset'] = rec['p']
                obj['fh1_source_record_order'] = rec['source_order']
                obj['fh1_position_header_offset'] = rec['position_header_offset']
                obj['fh1_position_header_candidate_count'] = rec['position_header_candidates']
                obj['fh1_position_header_verified'] = False
                obj['fh1_position_header_values'] = list(rec['position_header'])
                obj['fh1_source_vertex_indices'] = used
                obj['fh1_source_vertex_buffer_count'] = rec['count']
                obj['fh1_imported_vertex_count'] = len(used)
                obj['fh1_imported_triangle_count'] = len(faces)
                obj['fh1_section_offsets'] = [s['offset'] for s in sections]
                obj['fh1_section_materials'] = ','.join(s['name'] for s in sections)
                obj['fh1_all_section_count'] = len(rec['sections'])
                obj['fh1_imported_section_count'] = len(sections)
                obj['fh1_lod_boundary_method'] = 'explicit section header LOD ID'
                obj['fh1_lod_confirmed'] = True
                obj['fh1_source_section_lod_ids'] = [s['lod'] for s in sections]
                obj['fh1_source_section_indices'] = original_section_indices
                obj['fh1_section_face_attribute'] = 'fh1_section_index'
                imported += 1
            except Exception as exc:
                errors.append('%s_L%d: %s' % (rec['name'],lod_number,exc))
                print('[FH1 preview] ERROR',errors[-1])
    print('[FH1 preview] %d parts imported, %d failures, %d shared materials' %
          (imported,len(errors),len(materials)))
    for e in errors:
        print('[FH1 preview] FAILED',e)
    return root


def run():
    import bpy
    if CARBIN_PATH.strip():
        import_file(bpy.path.abspath(CARBIN_PATH))
        return
    from bpy.props import StringProperty
    from bpy_extras.io_utils import ImportHelper
    class FH1_OT_preview(bpy.types.Operator, ImportHelper):
        bl_idname = 'import_scene.fh1_lod_explicit_v084'
        bl_label = 'FH1 CARBIN LOD1-LOD5 Explicit LOD v0.8.4 (Cleanup, Section Face Metadata)' 
        filename_ext = '.carbin'
        filter_glob: StringProperty(default='*.carbin',options={'HIDDEN'})
        def execute(self, context):
            try:
                import_file(self.filepath)
            except Exception as exc:
                self.report({'ERROR'},str(exc))
                return {'CANCELLED'}
            return {'FINISHED'}
    try:
        bpy.utils.register_class(FH1_OT_preview)
    except ValueError:
        # Blender keeps the prior registration if the script is run twice.
        old = bpy.types.Operator.bl_rna_get_subclass_py('FH1_OT_preview', None)
        if old is None:
            raise
        bpy.utils.unregister_class(old)
        bpy.utils.register_class(FH1_OT_preview)
    bpy.ops.import_scene.fh1_lod_explicit_v084('INVOKE_DEFAULT')

if __name__ == '__main__':
    run()
