"""Experimental LOD1-5 position writer; re-encodes whole changed vertex buffers.

The FH1 decoder normalizes using decoded packed-coordinate extrema, so changing
one vertex can require re-encoding every vertex in that buffer.
"""
import hashlib
import math
import struct
from pathlib import Path
from . import export_carbin, import_lod

VERSION = '0.2.7'
DENOM = 32767.0 ** 2


def repack_record(result, original, rec, targets, tolerance=0.002):
    """Encode edited positions and update the bounds used by the FH1 decoder.

    The decoder maps the minimum and maximum packed coordinates onto the
    position header's dmin/dmax. Repacking without updating those header
    values *cannot* represent an edit outside the original bounds.
    """
    n = rec['count']
    old = [struct.unpack_from('>4h', original, rec['start'] + i * 28) for i in range(n)]
    wanted = [tuple(targets.get(i, rec['source_positions'][i])) for i in range(n)]
    header = rec['position_header']
    trans = header[:3]
    old_min, old_max = header[3:6], header[6:9]
    new_min = [min(v[k] for v in wanted) - trans[k] for k in range(3)]
    new_max = [max(v[k] for v in wanted) - trans[k] for k in range(3)]
    for k in range(3):
        if new_max[k] - new_min[k] < 1e-10:
            raise ValueError('[v%s] Degenerate bounds on axis %d of %s' % (VERSION, k, rec['name']))
    # Preserve the original packed dynamic range, including each vertex's W.
    # Only the XYZ signed shorts and six header floats are changed.
    out = [[old[i][k] for k in range(3)] for i in range(n)]
    for k in range(3):
        raw = [old[i][k] * old[i][3] / DENOM for i in range(n)]
        rawlo, rawhi = min(raw), max(raw)
        if rawhi - rawlo < 1e-12:
            raise ValueError('[v%s] Degenerate packed axis %d in %s' % (VERSION, k, rec['name']))
        for i in range(n):
            w = old[i][3]
            if not w:
                raise ValueError('[v%s] Zero W at %s vertex %d' % (VERSION, rec['name'], i))
            normalized = (wanted[i][k] - trans[k] - new_min[k]) / (new_max[k] - new_min[k])
            raw_target = rawlo + normalized * (rawhi - rawlo)
            packed = round(raw_target * DENOM / w)
            if not -32768 <= packed <= 32767:
                raise ValueError('[v%s] Packed range insufficient: %s axis %d vertex %d' % (VERSION, rec['name'], k, i))
            out[i][k] = packed
    for i in range(n):
        struct.pack_into('>3h', result, rec['start'] + i * 28, *out[i])
    # Header layout: 4-byte signature + 20 bytes, then 9 big-endian floats.
    # The fallback header has the same offset convention.
    header_offset = rec['position_header_offset'] + 20
    struct.pack_into('>6f', result, header_offset + 12, *(new_min + new_max))
    test_rec = dict(rec)
    test_rec['position_header'] = tuple(trans) + tuple(new_min) + tuple(new_max)
    actual = import_lod.decode_vertices(result, test_rec)[0]
    worst = (0.0, -1, -1)
    for i in range(n):
        for k in range(3):
            err = abs(actual[i][k] - wanted[i][k])
            if err > worst[0]:
                worst = (err, i, k)
    if worst[0] > tolerance:
        i, k = worst[1:]
        raise ValueError('[v%s] Quantization error %.6f in %s vertex %d axis %d; expected %.6f decoded %.6f (limit %.6f)' %
                         (VERSION, worst[0], rec['name'], i, k, wanted[i][k], actual[i][k], tolerance))
    return worst[0]


def export_positions(selected_obj, destination):
    import bpy
    from mathutils import Matrix
    source = Path(str(selected_obj.get('fh1_export_source_path', '')))
    if not source.is_file():
        raise ValueError('Select a mesh imported from an existing CARBIN')
    source = source.resolve()
    original = source.read_bytes()
    if hashlib.sha256(original).hexdigest() != selected_obj.get('fh1_export_source_sha256'):
        raise ValueError('Source CARBIN changed since import')
    target = Path(destination).resolve()
    if target == source:
        raise ValueError('Refusing to overwrite original')
    objects = [o for o in bpy.data.objects if o.get('fh1_export_source_path') == str(source)]
    expected = selected_obj.get('fh1_export_expected_object_count')
    if not objects or len(objects) != expected:
        raise ValueError('Tracked mesh count changed; export cancelled')
    records = {r['p']: r for r in import_lod.discover(original)}
    prev_end = 0
    for rec in sorted(records.values(), key=lambda r: r['p']):
        header_offset, vals, _ = import_lod.resolve_position_header(original, rec, prev_end)
        rec['position_header'] = vals
        rec['position_header_offset'] = header_offset
        rec['source_positions'] = import_lod.decode_vertices(original, rec)[0]
        prev_end = rec['end']
    # A shared header must not be rewritten independently by multiple buffers.
    changes = {}
    inverse_basis = Matrix.Rotation(math.radians(-90), 3, 'X')
    for obj in objects:
        if obj.type != 'MESH':
            raise ValueError('Tracked object is no longer a mesh: ' + obj.name)
        if 'fh1_source_lod' not in obj:
            raise ValueError('This exporter supports LOD1-5 only: ' + obj.name)
        mesh = obj.data
        indices = list(obj.get('fh1_source_vertex_indices', []))
        if len(indices) != len(mesh.vertices) or len(mesh.polygons) != obj.get('fh1_imported_triangle_count'):
            raise ValueError('Topology changed on %s; adding faces/vertices is not supported yet' % obj.name)
        if len(mesh.uv_layers) < 2:
            raise ValueError('Missing original UV layers: ' + obj.name)
        baseline = obj.get('fh1_export_nonposition_digest')
        if not baseline:
            raise ValueError('Missing v0.2.4 non-position snapshot; reimport source CARBIN: ' + obj.name)
        rec = records.get(int(obj.get('fh1_source_record_offset', -1)))
        if rec is None or rec['count'] != obj.get('fh1_source_vertex_buffer_count'):
            raise ValueError('Cannot locate source vertex buffer for ' + obj.name)
        if len(set(indices)) != len(indices):
            raise ValueError('Duplicate source index mapping: ' + obj.name)
        if export_carbin.nonposition_digest(obj) != baseline:
            raise ValueError('[v%s] Topology, UV, material or object transform changed on %s' % (VERSION, obj.name))
        for v, idx in zip(mesh.vertices, indices):
            position = tuple(inverse_basis @ v.co)
            old = rec['source_positions'][idx]
            if max(abs(position[k] - old[k]) for k in range(3)) <= 1e-6:
                continue
            key = (rec['p'], idx)
            if key in changes and max(abs(changes[key][0][k] - position[k]) for k in range(3)) > 1e-5:
                raise ValueError('Conflicting edits to shared source vertex %s (across LODs)' % (key,))
            changes[key] = (position, rec)
    result = bytearray(original)
    grouped = {}
    for (record_offset, idx), (position, rec) in changes.items():
        grouped.setdefault(record_offset, {})[idx] = position
    header_users = {}
    for record_offset in grouped:
        h = records[record_offset]['position_header_offset']
        header_users.setdefault(h, []).append(record_offset)
    for h, offsets in header_users.items():
        if len(offsets) > 1:
            raise ValueError('[v%s] Shared position header at %d across edited buffers %s; refusing unsafe export' % (VERSION, h, offsets))
    # Also reject rewriting a header used by an *unchanged* buffer.
    for record_offset in grouped:
        h = records[record_offset]['position_header_offset']
        other = [r['p'] for r in records.values() if r['p'] != record_offset and r['position_header_offset'] == h]
        if other:
            raise ValueError('[v%s] Position header shared with buffers %s; cannot independently update bounds' % (VERSION, other))
    errors = []
    for record_offset, targets in grouped.items():
        errors.append(repack_record(result, original, records[record_offset], targets))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result)
    if target.read_bytes() != bytes(result):
        raise IOError('Output verification failed')
    print('[FH1 v%s] Re-encoded %d buffers; max decode error %.6f' % (VERSION, len(grouped), max(errors, default=0.0)))
    return len(changes), len(result), hashlib.sha256(result).hexdigest()
