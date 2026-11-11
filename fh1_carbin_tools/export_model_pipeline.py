"""Dev 6: model-scoped LOD1-5 export, preserving separate imported components.

One topology-bearing component is currently supported; other components may
have original vertex position edits. Never silently omit unsupported changes.
"""
import hashlib
import math
import os
import tempfile
from pathlib import Path

from . import import_lod, export_topology, export_positions, export_carbin, lod_isolation, secondary_lod_writer


def export_model(active, destination):
    import bpy
    from mathutils import Matrix
    if active is None or active.type != 'MESH':
        raise ValueError('Select an imported FH1 mesh to identify the model')
    source = Path(str(active.get('fh1_export_source_path', ''))).resolve()
    if not source.is_file():
        raise ValueError('Selected mesh has no tracked CARBIN source')
    original = source.read_bytes()
    sha = hashlib.sha256(original).hexdigest()
    if sha != active.get('fh1_export_source_sha256'):
        raise ValueError('Source CARBIN changed since import')
    dest = Path(destination).resolve()
    if dest == source:
        raise ValueError('Refusing to overwrite original CARBIN')
    objects = [o for o in bpy.data.objects if o.type == 'MESH' and
               o.get('fh1_export_source_path') == str(source) and
               o.get('fh1_export_source_sha256') == sha]
    if not objects:
        raise ValueError('No imported FH1 components found')
    if any('fh1_source_lod' not in o for o in objects):
        raise ValueError('LOD0/cockpit export is not supported yet')
    lod = int(active['fh1_source_lod'])
    relevant = [o for o in objects if int(o['fh1_source_lod']) == lod]
    # Require every original component of this LOD to still exist.
    source_records = import_lod.discover(original)
    expected = {(r['p'], lod) for r in source_records if any(s['lod'] == lod for s in r['sections'])}
    actual = {(int(o['fh1_source_record_offset']), int(o['fh1_source_lod'])) for o in relevant}
    if actual != expected or len(relevant) != len(expected):
        raise ValueError('Original LOD component missing or duplicated; cannot safely export')
    additions = []
    for obj in relevant:
        mesh = obj.data
        ids = mesh.attributes.get('fh1_original_vertex_id')
        faces = mesh.attributes.get('fh1_original_section_id')
        if ids is None or faces is None:
            raise ValueError('Missing provenance on ' + obj.name)
        if any(a.value == 0 for a in faces.data) or any(a.value == 0 for a in ids.data):
            additions.append(obj)
    if len(additions) > 1:
        raise ValueError('Added geometry exists in multiple components; multi-component topology rebuild is not yet safe')
    if not additions:
        raise ValueError('No new geometry found; position-only model export is not implemented in this pipeline yet')
    primary = additions[0]
    # All secondary components must preserve original topology/UVs/materials.
    # Source record offsets shift after topology insertion, so map by record order.
    for obj in relevant:
        if obj is primary:
            continue
        baseline = obj.get('fh1_export_nonposition_digest')
        if not baseline or export_carbin.nonposition_digest(obj) != baseline:
            raise ValueError('Non-position edit in %s is unsupported; no changes written' % obj.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='fh1_dev11_', suffix='.carbin')
    os.close(fd)
    try:
        new_v, new_f, error, size, primary_moved = export_topology.export_joined(primary, temporary)
        result = bytearray(Path(temporary).read_bytes())
        rebuilt = import_lod.discover(result)
        if len(rebuilt) != len(source_records):
            raise ValueError('Record count changed during topology rebuild')
        inv = Matrix.Rotation(-math.pi/2, 3, 'X')
        moved = primary_moved
        record_orders = {r['p']: i for i, r in enumerate(source_records)}
        for obj in relevant:
            if obj is primary:
                continue
            rec_offset = int(obj['fh1_source_record_offset'])
            order = record_orders.get(rec_offset)
            if order is None:
                raise ValueError('Missing source record for ' + obj.name)
            rec = rebuilt[order]
            if rec['name'] != source_records[order]['name'] or rec['count'] != source_records[order]['count']:
                raise ValueError('Source record identity changed for ' + obj.name)
            # Resolve headers in the rebuilt file, including shifted offsets.
            preceding = rebuilt[order-1]['end'] if order else 0
            h, vals, _ = import_lod.resolve_position_header(result, rec, preceding)
            rec['position_header_offset'] = h
            rec['position_header'] = vals
            rec['source_positions'] = import_lod.decode_vertices(result, rec)[0]
            targets = {}
            ids = obj.data.attributes['fh1_original_vertex_id']
            if len(ids.data) != len(obj.data.vertices):
                raise ValueError('Invalid vertex provenance on ' + obj.name)
            for vi, attr in enumerate(ids.data):
                idx = int(attr.value)-1
                if idx < 0 or idx >= rec['count']:
                    raise ValueError('Invalid original vertex ID on ' + obj.name)
                pos = tuple(inv @ obj.data.vertices[vi].co)
                old = rec['source_positions'][idx]
                if max(abs(pos[k]-old[k]) for k in range(3)) > lod_isolation.POSITION_CHANGE_EPSILON:
                    targets[idx] = pos
            if targets:
                # Two different LOD objects can share the same vertex record;
                # refuse a partial update rather than overwriting a sibling.
                siblings = [o for o in relevant if o is not obj and
                            int(o['fh1_source_record_offset']) == rec_offset]
                if siblings:
                    raise ValueError('Shared vertex buffer across components; needs coordinated repack')
                result, component_error = secondary_lod_writer.apply(result, order, lod, targets)
                rebuilt = import_lod.discover(result)
                error = max(error, component_error)
                moved += len(targets)
        # Verify the final output is structurally parseable before committing.
        final_records = import_lod.discover(result)
        if len(final_records) != len(source_records):
            raise ValueError('Final CARBIN parser validation failed')
        Path(temporary).write_bytes(result)
        os.replace(temporary, dest)
        return new_v, new_f, error, len(result), moved
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
