"""Strict lossless baseline exporter. Edited geometry is intentionally unsupported."""
import hashlib
import os
from pathlib import Path


def mesh_digest(obj):
    """Fingerprint imported mesh topology, geometry, UVs and section identity."""
    h = hashlib.sha256()
    mesh = obj.data
    def add(s): h.update((str(s) + '\n').encode('utf-8'))
    add(len(mesh.vertices)); add(len(mesh.polygons)); add(len(mesh.loops))
    for v in mesh.vertices:
        add(tuple(round(float(x), 7) for x in v.co))
    for p in mesh.polygons:
        add(tuple(p.vertices)); add(p.material_index)
    for layer in mesh.uv_layers:
        add(layer.name)
        for uv in layer.data:
            add(tuple(round(float(x), 7) for x in uv.uv))
    attr = mesh.attributes.get('fh1_section_index')
    if attr is None:
        add('NO_SECTION_ATTRIBUTE')
    else:
        for entry in attr.data: add(entry.value)
    for mat in mesh.materials:
        add((mat.get('fh1_shader_name', mat.name) if mat else 'NONE'))
    add(tuple(round(float(x), 7) for row in obj.matrix_world for x in row))
    return h.hexdigest()


def nonposition_digest(obj):
    """Fingerprint topology, UVs, materials and source identity, excluding coordinates.

    This deliberately excludes vertex positions, unlike mesh_digest(), so
    legitimate Edit Mode deformations do not require a lossy restore/roundtrip.
    """
    h = hashlib.sha256()
    mesh = obj.data
    def add(value):
        h.update((str(value) + '\n').encode('utf-8'))
    add((len(mesh.vertices), len(mesh.polygons), len(mesh.loops)))
    for poly in mesh.polygons:
        add((tuple(poly.vertices), poly.material_index))
    for layer in mesh.uv_layers:
        add(layer.name)
        for uv in layer.data:
            add(tuple(round(float(x), 6) for x in uv.uv))
    attr = mesh.attributes.get('fh1_section_index')
    if attr is None:
        add('NO_SECTION_ATTRIBUTE')
    else:
        for entry in attr.data:
            add(entry.value)
    for mat in mesh.materials:
        add(mat.get('fh1_shader_name', mat.name) if mat else 'NONE')
    add(tuple(round(float(x), 7) for row in obj.matrix_world for x in row))
    return h.hexdigest()


def snapshot_import(root, path, objects):
    source = str(Path(path).resolve())
    raw = Path(source).read_bytes()
    for obj in objects:
        if obj.type == 'MESH':
            obj['fh1_export_baseline_digest'] = mesh_digest(obj)
            obj['fh1_export_nonposition_digest'] = nonposition_digest(obj)
            obj['fh1_export_source_path'] = source
            obj['fh1_export_source_sha256'] = hashlib.sha256(raw).hexdigest()
            obj['fh1_export_expected_object_count'] = len(objects)


def lossless_export(selected_obj, destination):
    """Lossless baseline only. Refuses modified geometry and added meshes."""
    import bpy
    source = Path(selected_obj.get('fh1_export_source_path', ''))
    if not source.is_file():
        raise ValueError('Select a mesh imported with this version; source file must exist')
    original = source.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest != selected_obj.get('fh1_export_source_sha256'):
        raise ValueError('Original CARBIN bytes changed since import')
    objects = [o for o in bpy.data.objects if o.get('fh1_export_source_path') == str(source)]
    if not objects or len(objects) != selected_obj.get('fh1_export_expected_object_count', -1):
        raise ValueError('Imported mesh count changed; refusing lossless export')
    for obj in objects:
        baseline = obj.get('fh1_export_baseline_digest')
        if not baseline or obj.type != 'MESH' or mesh_digest(obj) != baseline:
            raise ValueError('Modified mesh: %s. Geometry writing is not yet implemented.' % obj.name)
    # A newly added object in the same component collection is an edit too.
    for obj in objects:
        for collection in obj.users_collection:
            for other in collection.objects:
                if other.type == 'MESH' and other not in objects:
                    raise ValueError('New mesh detected: %s. Geometry writing is not yet implemented.' % other.name)
    target = Path(destination).resolve()
    if target == source:
        raise ValueError('Refusing to overwrite original source')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(original)
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise IOError('Export verification failed')
    return len(original), digest
