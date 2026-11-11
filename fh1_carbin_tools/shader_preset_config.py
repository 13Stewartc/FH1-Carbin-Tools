"""Editable Blender 3.6 Principled preview presets; no FH1 binary writes."""
import json
import os
import colorsys

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'shader_presets.json')

def load_config(path=CONFIG_PATH):
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if data.get('version') != 1 or not isinstance(data.get('shaders'), dict):
        raise ValueError('shader_presets.json must have version 1 and a shaders object')
    return data

def _linear(v):
    return v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4

def color_value(value):
    if 'hex' in value:
        h = value['hex'].lstrip('#')
        if len(h) not in (6, 8): raise ValueError('hex must be #RRGGBB or #RRGGBBAA')
        channels = [int(h[i:i+2], 16) / 255 for i in range(0, len(h), 2)]
    elif 'rgb' in value or 'rgba' in value:
        channels = value.get('rgb', value.get('rgba'))
        if len(channels) not in (3, 4): raise ValueError('RGB needs 3 or 4 channels')
        if any(not isinstance(v, (float, int)) or v < 0 or v > 255 for v in channels):
            raise ValueError('RGB channels must be in 0..255')
        channels = [v / 255 for v in channels]
    elif 'hsv' in value:
        h, s, v = value['hsv']
        if not (0 <= h <= 360 and 0 <= s <= 1 and 0 <= v <= 1):
            raise ValueError('HSV expects degrees 0..360, saturation/value 0..1')
        channels = list(colorsys.hsv_to_rgb(h / 360, s, v))
    else:
        raise ValueError('Expected hex, rgb, rgba or hsv')
    alpha = channels[3] if len(channels) == 4 else 1.0
    return tuple(_linear(v) for v in channels[:3]) + (alpha,)

def apply_preset(mat, bsdf, shader, *, family=None, config=None):
    """Apply configured values only; null preserves Blender defaults / existing links.

    Explicit texture references are reserved for the upcoming XDS picker and are
    deliberately not treated as paths to Blender-native images.
    """
    if bsdf is None: return 0
    data = config if config is not None else load_config()
    entries = data['shaders']
    keys = [family, shader.lower().removeprefix('cockpit_')]
    applied = 0
    for key in keys:
        entry = entries.get(key) if key else None
        if not entry: continue
        for socket_name, value in entry.get('principled', {}).items():
            if value is None: continue
            socket = bsdf.inputs.get(socket_name)
            if socket is None:
                print('[FH1 preset] Unknown Principled socket %r for %s' % (socket_name, key))
                continue
            if socket.is_linked: continue
            if isinstance(value, dict):
                if 'texture' in value:
                    print('[FH1 preset] Texture override %r deferred until XDS assignment support' % socket_name)
                    continue
                value = color_value(value) if any(k in value for k in ('hex','rgb','rgba','hsv')) else value
            try:
                socket.default_value = value
                applied += 1
            except (TypeError, ValueError) as exc:
                print('[FH1 preset] Invalid %s/%s: %s' % (key, socket_name, exc))
    return applied
