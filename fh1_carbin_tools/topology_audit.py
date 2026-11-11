"""v0.2.8: audit joined Blender meshes before topology serialization.

This does not write new geometry into CARBIN. It makes the import retain
vertex/face provenance as mesh attributes that survive Blender Ctrl+J.
"""
from pathlib import Path
import hashlib

VERSION = '0.2.8'


def audit(selected):
    import bpy
    if selected is None or selected.type != 'MESH':
        raise ValueError('Select the original FH1 mesh (the active Ctrl+J object)')
    source = selected.get('fh1_export_source_path')
    if not source or not Path(source).is_file():
        raise ValueError('Active object has no FH1 source metadata. Join INTO the imported FH1 object, not the plate.')
    if hashlib.sha256(Path(source).read_bytes()).hexdigest() != selected.get('fh1_export_source_sha256'):
        raise ValueError('Source CARBIN has changed since import')
    mesh = selected.data
    src = mesh.attributes.get('fh1_original_vertex_id')
    sec = mesh.attributes.get('fh1_original_section_id')
    if src is None or src.domain != 'POINT' or sec is None or sec.domain != 'FACE':
        raise ValueError('Missing v0.2.8 provenance attributes. Reimport the CARBIN using v0.2.8 before joining.')
    original_vertices = [v.value for v in src.data if v.value > 0]
    added_vertices = [i for i, v in enumerate(src.data) if v.value == 0]
    original_faces = sum(v.value > 0 for v in sec.data)
    added_faces = [i for i, v in enumerate(sec.data) if v.value == 0]
    invalid = [v for v in original_vertices if v < 1 or v > selected['fh1_source_vertex_buffer_count']]
    duplicates = len(original_vertices) - len(set(original_vertices))
    unknown_sections = [v.value for v in sec.data if v.value > selected.get('fh1_all_section_count', 0)]
    print('[FH1 v%s] %s: original vertices=%d, added vertices=%d, original faces=%d, added faces=%d' %
          (VERSION, selected.name, len(original_vertices), len(added_vertices), original_faces, len(added_faces)))
    if invalid or duplicates or unknown_sections:
        raise ValueError('Provenance conflict: invalid vertices=%d, duplicate source IDs=%d, invalid sections=%d' %
                         (len(invalid), duplicates, len(unknown_sections)))
    if added_faces and not added_vertices:
        print('[FH1 v%s] New faces reuse original vertices; topology reconstruction still required.' % VERSION)
    return len(added_vertices), len(added_faces), len(original_vertices), original_faces
