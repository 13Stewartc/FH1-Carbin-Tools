"""Safety checks for source vertices referenced by multiple LODs."""

POSITION_CHANGE_EPSILON = 0.002

def shared_lod_vertices(record, edited_lod):
    selected = set()
    other = set()
    for section in record['sections']:
        target = selected if section['lod'] == edited_lod else other
        for face in section['faces']:
            target.update(int(i) for i in face)
    return selected & other

def check_changes(record, lod, changes):
    shared = shared_lod_vertices(record, lod)
    conflicts = sorted(shared.intersection(changes))
    if conflicts:
        raise ValueError(
            'LOD isolation: %d edited vertices in %s LOD%d are also used by other LODs '
            '(first source IDs: %s). Export cancelled without writing: these vertices '
            'need duplication and LOD-specific index remapping.' % (
                len(conflicts), record['name'], lod, ', '.join(map(str, conflicts[:8]))))
