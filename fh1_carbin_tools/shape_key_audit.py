"""Read-only authored damage displacement analysis. No CARBIN encoding claims."""
import math

def audit(obj, name):
    keys = obj.data.shape_keys
    if not keys or name not in keys.key_blocks:
        raise ValueError('Shape key not found on selected mesh: ' + name)
    basis = keys.reference_key
    target = keys.key_blocks[name]
    if target == basis:
        raise ValueError('Select a non-Basis damage shape')
    if len(target.data) != len(obj.data.vertices):
        raise ValueError('Shape key vertex count mismatch')
    changed = []
    for i in range(len(target.data)):
        d = target.data[i].co - basis.data[i].co
        magnitude = d.length
        if magnitude > 1e-6:
            changed.append((i, magnitude))
    source_ids = obj.data.attributes.get('fh1_source_vertex_id')
    # Only inspect provenance when known; don't assume attribute names.
    groups = [g.name for g in obj.vertex_groups]
    return {'object': obj.name, 'shape': name, 'basis': basis.name,
            'total_vertices': len(target.data), 'changed_vertices': len(changed),
            'maximum_displacement': max((x[1] for x in changed), default=0.0),
            'sample_indices': [x[0] for x in changed[:12]],
            'vertex_groups': groups,
            'note': 'READ ONLY. Existing donor/zero secondary encoding remains unchanged.'}
