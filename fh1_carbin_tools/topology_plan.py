"""v0.3.0: prepare a deterministic, source-aware geometry rebuild plan.

No CARBIN writes: reconstruction requires verified offset/size relationships.
"""
import hashlib
import json
from pathlib import Path
from . import import_lod

VERSION = '0.3.0'


def build_plan(obj):
    if obj is None or obj.type != 'MESH':
        raise ValueError('Select the original FH1 mesh after Ctrl+J')
    path = Path(str(obj.get('fh1_export_source_path', '')))
    if not path.is_file():
        raise ValueError('Missing original CARBIN source')
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != obj.get('fh1_export_source_sha256'):
        raise ValueError('Original CARBIN SHA256 changed since import')
    if 'fh1_source_lod' not in obj:
        raise ValueError('Topology planning currently supports LOD1-5 only')
    mesh = obj.data
    ids = mesh.attributes.get('fh1_original_vertex_id')
    secs = mesh.attributes.get('fh1_original_section_id')
    if ids is None or ids.domain != 'POINT' or secs is None or secs.domain != 'FACE':
        raise ValueError('Missing provenance: fresh-import using v0.2.8+ before Ctrl+J')
    records = {r['p']: r for r in import_lod.discover(raw)}
    rec = records.get(int(obj.get('fh1_source_record_offset', -1)))
    if rec is None:
        raise ValueError('Original CARBIN vertex record not found')
    source_ids = [a.value for a in ids.data]
    existing = [x for x in source_ids if x > 0]
    if len(existing) != len(set(existing)) or any(x > rec['count'] for x in existing):
        raise ValueError('Invalid or duplicate original vertex IDs')
    # The imported mesh may omit vertices unused by this LOD. Keep original
    # indices untouched and allocate new indices AFTER the entire source buffer.
    new_indices = {i: rec['count'] + n for n, i in enumerate(i for i, x in enumerate(source_ids) if x == 0)}
    if rec['count'] + len(new_indices) > 65535:
        raise ValueError('16-bit index limit exceeded; section splitting needed')
    remap = {i: (x-1 if x else new_indices[i]) for i, x in enumerate(source_ids)}
    source_section_indices = list(obj.get('fh1_source_section_indices', []))
    if not source_section_indices:
        raise ValueError('Missing source section mapping')
    section_by_material = {}
    for mat_index, section_index in enumerate(source_section_indices):
        section_by_material.setdefault(mat_index, section_index)
    additions = {str(i): [] for i in source_section_indices}
    old_faces = 0
    new_faces = 0
    for poly in mesh.polygons:
        provenance = secs.data[poly.index].value
        if provenance > 0:
            old_faces += 1
            continue
        new_faces += 1
        if poly.material_index not in section_by_material:
            raise ValueError('New face %d uses material slot %d without an original FH1 section' % (poly.index, poly.material_index))
        target_section = section_by_material[poly.material_index]
        # Fan triangulation is correct for convex planar faces; require
        # explicit triangulation for concave/ngon geometry to avoid ambiguity.
        if len(poly.vertices) != 3:
            raise ValueError('New face %d has %d corners; triangulate new faces before export' % (poly.index, len(poly.vertices)))
        tri = [remap[v] for v in poly.vertices]
        if len(set(tri)) != 3:
            raise ValueError('Degenerate new triangle %d' % poly.index)
        additions[str(target_section)].append(tri)
    if not new_faces:
        raise ValueError('No new faces detected on the selected mesh')
    new_vertices = []
    for vi, new_id in new_indices.items():
        vertex = mesh.vertices[vi]
        new_vertices.append({'blender_vertex': vi, 'carbin_vertex': new_id,
                             'position_blender': list(vertex.co)})
    return {'format': 'fh1-topology-plan-v1', 'source': str(path), 'sha256': digest,
            'part': obj.name, 'lod': int(obj['fh1_source_lod']),
            'record_offset': rec['p'], 'original_buffer_vertex_count': rec['count'],
            'new_vertex_count': len(new_indices), 'original_faces_in_part': old_faces,
            'new_faces': new_faces, 'new_vertices': new_vertices,
            'new_triangles_by_source_section': additions,
            'status': 'PLAN ONLY — NOT AN EXPORTED CARBIN'}


def write_plan(obj, filepath):
    plan = build_plan(obj)
    dest = Path(filepath).resolve()
    if dest.suffix.lower() != '.json':
        raise ValueError('Topology plan must be saved as .json')
    dest.write_text(json.dumps(plan, indent=2), encoding='utf-8')
    return plan
