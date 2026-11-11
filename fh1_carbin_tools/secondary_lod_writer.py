"""Apply position edits to a separate LOD component, isolating shared indices."""
import struct
from . import import_lod, export_positions, lod_isolation


def apply(data, record_order, lod, targets):
    if not targets:
        return data, 0.0
    source = bytes(data)
    records = import_lod.discover(source)
    rec = records[record_order]
    shared = lod_isolation.shared_lod_vertices(rec, lod)
    isolated_ids = sorted(set(targets) & shared)
    if rec['count'] + len(isolated_ids) >= 65535:
        raise ValueError('16-bit vertex limit exceeded during LOD isolation')
    # Work on a single complete buffer at a time, maintaining every other
    # section's original indices and packed vertex records.
    result = bytearray(source)
    new_targets = dict(targets)
    for i, old_id in enumerate(isolated_ids):
        new_id = rec['count'] + i
        new_targets[new_id] = new_targets.pop(old_id)
        for section in rec['sections']:
            if section['lod'] != lod:
                continue
            off = section['offset']
            name_len = struct.unpack_from('>I', source, off)[0]
            hdr = off + 4 + name_len
            count = struct.unpack_from('>I', source, hdr + 72)[0]
            for k in range(count):
                pos = hdr + 80 + 2*k
                if struct.unpack_from('>H', source, pos)[0] == old_id:
                    struct.pack_into('>H', result, pos, new_id)
    extra = b''.join(source[rec['start'] + i*28:rec['start'] + (i+1)*28] for i in isolated_ids)
    if extra:
        struct.pack_into('>I', result, rec['start']-16, rec['count']+len(isolated_ids))
        result[rec['end']:rec['end']] = extra
    updated = import_lod.discover(result)
    if len(updated) != len(records):
        raise ValueError('Record count changed after LOD isolation')
    rec2 = updated[record_order]
    if rec2['count'] != rec['count']+len(isolated_ids):
        raise ValueError('Vertex count validation failed after LOD isolation')
    previous = updated[record_order-1]['end'] if record_order else 0
    header, vals, _ = import_lod.resolve_position_header(result, rec2, previous)
    rec2['position_header_offset'] = header
    rec2['position_header'] = vals
    rec2['source_positions'] = import_lod.decode_vertices(result, rec2)[0]
    # Preserve untouched positions and encode only the requested target set.
    error = export_positions.repack_record(result, bytes(result), rec2, new_targets)
    final = import_lod.discover(result)
    if len(final) != len(records) or final[record_order]['count'] != rec2['count']:
        raise ValueError('Final isolated component failed parser validation')
    return result, error
