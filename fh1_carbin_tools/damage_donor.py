"""Experimental nearest-original-vertex secondary record assignment.

No claimed damage encoding. Copy exact four-byte source records only.
"""
import math
from contextlib import contextmanager

_active_selection = None

@contextmanager
def export_selection(selection):
    global _active_selection
    previous = _active_selection
    _active_selection = selection or "SAME"
    try:
        yield
    finally:
        _active_selection = previous

def selected_entry():
    return _active_selection or "SAME"



def enabled():
    if _active_selection is not None:
        return _active_selection != "OFF"
    try:
        import bpy
        addon = bpy.context.preferences.addons.get(__package__)
        return bool(addon and addon.preferences.damage_copy_nearest)
    except (ImportError, AttributeError):
        return False


def nearest_records(source_positions, source_records, target_positions):
    if not source_positions or len(source_positions) != len(source_records):
        raise ValueError('Invalid secondary donor source')
    if any(len(rec) != 4 for rec in source_records):
        raise ValueError('Secondary donor record is not four bytes')
    # Simple deterministic nearest search: number of new plate vertices is small.
    output=[]
    for p in target_positions:
        best=min(range(len(source_positions)), key=lambda i: sum((float(p[k])-float(source_positions[i][k]))**2 for k in range(3)))
        output.append(source_records[best])
    return b''.join(output)


def donor_mode():
    if _active_selection is not None:
        return _active_selection
    try:
        import bpy
        addon = bpy.context.preferences.addons.get(__package__)
        return getattr(addon.preferences, 'damage_donor_mode', 'SAME') if addon else 'SAME'
    except (ImportError, AttributeError):
        return 'SAME'


def choose_rear_bumper(candidates, own_name):
    """Choose an explicit rear bumper; never silently substitute the trunk."""
    import re
    matches = []
    for item in candidates:
        name = item['name']
        if name == own_name:
            continue
        normalized = re.sub(r'[^a-z0-9]', '', name.lower())
        if normalized.startswith(('bumperr', 'rearbumper', 'bumperrear')):
            matches.append(item)
    if not matches:
        raise ValueError('Dev18 rear-bumper donor not found in source CARBIN; export cancelled (no fallback to trunk)')
    matches.sort(key=lambda x: (0 if x['name'].lower() == 'bumperra' else 1 if x['name'].lower() == 'bumperr' else 2 if x['name'].lower().startswith('bumperr') and 'frame' not in x['name'].lower() else 3, len(x['name']), x['name']))
    return matches[0]
