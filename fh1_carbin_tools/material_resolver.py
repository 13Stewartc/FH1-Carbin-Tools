"""Experimental FH1 shader/sampler resolver. Does not alter geometry parsers."""
import os
import time
import re
import xml.etree.ElementTree as ET
from . import xds_textures


def _xml(path):
    with open(path, 'rb') as f: raw=f.read()
    # Original game XML uses bare ampersands in loadFlags (e.g. Cockpit & !SuperLOD).
    raw=re.sub(rb'&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z]+;)', b'&amp;', raw)
    return ET.fromstring(raw)


def _find(root, *names, index=None):
    if not root or not os.path.isdir(root): return None
    for name in names:
        p=os.path.join(root,name)
        if os.path.isfile(p): return p
    if index is not None:
        for name in names:
            matches = index.get(os.path.basename(name).lower(), ())
            if matches: return matches[0]
        return None
    for directory, _, files in os.walk(root):
        for f in files:
            if f.lower() in {n.lower() for n in names}:
                return os.path.join(directory,f)
    return None


def _file_index(root):
    """One directory traversal per root; deterministic filename fallback."""
    result = {}
    if root and os.path.isdir(root):
        for directory, dirs, files in os.walk(root):
            dirs.sort()
            for filename in sorted(files):
                result.setdefault(filename.lower(), []).append(os.path.join(directory, filename))
    return result


def _merge(base, overrides, name):
    # Override properties individually, as XML comments specify override semantics.
    props={c.tag:c for c in base.find(name)} if base.find(name) is not None else {}
    if overrides is not None and overrides.find(name) is not None:
        props.update({c.tag:c for c in overrides.find(name)})
    return props


def _match_flags(flags, cockpit, superlod, lod0=False, ingame_textures=True):
    """Match all recognized FH1 texture load-flag predicates.

    Unknown flags fail closed rather than silently selecting a wrong texture.
    """
    states = {'cockpit': bool(cockpit), 'superlod': bool(superlod),
              'lod0': bool(lod0), 'lod15': not bool(lod0) and not bool(superlod),
              'ingametextures': bool(ingame_textures)}
    for token in flags.lower().split('&'):
        token = token.strip()
        if not token: continue
        negative = token.startswith('!')
        key = token[1:] if negative else token
        if key not in states or states[key] == negative:
            return False
    return True


def resolve_materials(carbin_path, shared_root, *, cockpit=False, superlod=False):
    """Return material-name -> sampler candidates, with verified existing file paths."""
    import bpy
    car_root=os.path.dirname(os.path.abspath(carbin_path))
    lod0 = cockpit or ("lod0" in os.path.basename(carbin_path).lower())
    normal_path=_find(shared_root, 'ShaderSettings/Normal.xml')
    texture_path=_find(shared_root, 'textures.xml')
    if not normal_path or not texture_path:
        raise ValueError('Shared folder needs ShaderSettings/Normal.xml and textures.xml')
    base=_xml(normal_path); tex=_xml(texture_path)
    car_index=_file_index(car_root)
    shared_index=_file_index(shared_root)
    vehicle_path=_find(car_root,'ShaderSettings.XML',index=car_index)
    overrides=_xml(vehicle_path) if vehicle_path else None
    results={}
    for mat in bpy.data.materials:
        if not mat.name.startswith('FH1_'): continue
        name=mat.get('fh1_shader_name', mat.name[4:])
        if name.lower().startswith('fh1_'): name=name[4:]
        shader=name if base.find(name) is not None else None
        if cockpit and base.find('cockpit_'+name) is not None:
            shader='cockpit_'+name
        if not shader and overrides is not None and overrides.find(name) is not None:
            shader=name
        if not shader: shader=name  # allow JSON-only preview rules
        props=_merge(base,overrides,shader)
        # Vehicle overrides can use unprefixed material identifiers.
        if shader!=name and overrides is not None and overrides.find(name) is not None:
            props.update({c.tag:c for c in overrides.find(name)})
        bindings=[]
        for sampler in props:
            if not sampler.endswith('Sampler'): continue
            node=tex.find(sampler)
            if node is None: continue
            options=[]
            for t in node.findall('Texture'):
                if not _match_flags(t.get('loadFlags',''),cockpit,superlod,lod0=lod0): continue
                location=t.get('searchPath','').upper()
                root=shared_root if 'SHAREDCARFOLDER' in location else car_root if 'CARFOLDER' in location else None
                if root is None: continue
                path=_find(root,t.get('value',''),index=(shared_index if root == shared_root else car_index))
                if path: options.append(path)
            if options: bindings.append((sampler,options[0]))
        results[mat.name]=(shader,bindings)
    return results


def _wire_bc5_normal(mat, tex_node, bsdf, sampler):
    """Reconstruct BC5 XY -> tangent-space XYZ and connect Blender Normal Map.

    XDS BC5's blue channel is unused. Feeding raw RGB directly to a normal
    map node is incorrect; calculate Z = sqrt(max(0, 1 - X*X - Y*Y)).
    """
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    prefix = 'FH1 BC5 | ' + sampler + ' | '
    # Re-running material resolution should reuse existing nodes.
    def make(kind, suffix, x, y):
        n = nodes.get(prefix + suffix)
        if n is None or n.bl_idname != kind:
            if n is not None: nodes.remove(n)
            n = nodes.new(kind)
        n.name = prefix + suffix
        n.label = 'BC5 ' + suffix
        n.location = (x, y)
        return n
    sep = make('ShaderNodeSeparateRGB', 'Separate XY', -390, -420)
    links.new(tex_node.outputs['Color'], sep.inputs['Image'])
    def math_node(label, op, x, y, first=None, second=None):
        n = make('ShaderNodeMath', label, x, y)
        n.operation = op
        if first is not None: n.inputs[0].default_value = first
        if second is not None: n.inputs[1].default_value = second
        return n
    xm = math_node('X times 2', 'MULTIPLY', -180, -350, second=2.0)
    xs = math_node('X signed', 'SUBTRACT', 10, -350, second=1.0)
    ym = math_node('Y times 2', 'MULTIPLY', -180, -520, second=2.0)
    ys = math_node('Y signed', 'SUBTRACT', 10, -520, second=1.0)
    links.new(sep.outputs['R'], xm.inputs[0]); links.new(xm.outputs[0], xs.inputs[0])
    links.new(sep.outputs['G'], ym.inputs[0]); links.new(ym.outputs[0], ys.inputs[0])
    x2 = math_node('X squared', 'MULTIPLY', 190, -350)
    y2 = math_node('Y squared', 'MULTIPLY', 190, -520)
    links.new(xs.outputs[0], x2.inputs[0]); links.new(xs.outputs[0], x2.inputs[1])
    links.new(ys.outputs[0], y2.inputs[0]); links.new(ys.outputs[0], y2.inputs[1])
    xy = math_node('X2 plus Y2', 'ADD', 370, -430)
    links.new(x2.outputs[0], xy.inputs[0]); links.new(y2.outputs[0], xy.inputs[1])
    z2 = math_node('1 minus XY2', 'SUBTRACT', 540, -430, first=1.0)
    links.new(xy.outputs[0], z2.inputs[1])
    clamped = math_node('Clamp Z2', 'MAXIMUM', 710, -430, second=0.0)
    links.new(z2.outputs[0], clamped.inputs[0])
    z = math_node('Z square root', 'SQRT', 880, -430)
    links.new(clamped.outputs[0], z.inputs[0])
    zm = math_node('Z times 0.5', 'MULTIPLY', 1050, -430, second=0.5)
    za = math_node('Z to colour', 'ADD', 1220, -430, second=0.5)
    links.new(z.outputs[0], zm.inputs[0]); links.new(zm.outputs[0], za.inputs[0])
    combine = make('ShaderNodeCombineRGB', 'Reconstructed RGB', 1400, -250)
    links.new(sep.outputs['R'], combine.inputs['R'])
    links.new(sep.outputs['G'], combine.inputs['G'])
    links.new(za.outputs[0], combine.inputs['B'])
    normal = make('ShaderNodeNormalMap', 'Tangent normal', 1610, -250)
    normal.space = 'TANGENT'
    links.new(combine.outputs['Image'], normal.inputs['Color'])
    links.new(normal.outputs['Normal'], bsdf.inputs['Normal'])




# Blender-friendly approximations by FH1 shader family. These are NOT literal
# conversions of Forza's lighting equations; tune against in-game references.
_SHADER_PRESETS = {
    'leather':       dict(roughness=.64, metallic=0.0, specular=.16),
    'plastic':       dict(roughness=.330, metallic=0.0, specular=.035),
    'dullplastic':   dict(roughness=.78, metallic=0.0, specular=.16),
    'rubber':        dict(roughness=.83, metallic=0.0, specular=.22),
    'cloth':         dict(roughness=.88, metallic=0.0, specular=.23),
    'carbon_fiber':  dict(roughness=.36, metallic=0.0, specular=.48),
    'brushed_metal': dict(roughness=.34, metallic=.86, specular=.50),
    'metal':         dict(roughness=.30, metallic=1.0, specular=.50),
    'glass':         dict(roughness=.08, metallic=0.0, specular=1.0, transmission=.75, ior=1.45, base_color=(0.04970656598412723, 0.04970656598412723, 0.04970656598412723, 1.0)),
    'chrome':        dict(roughness=.150, metallic=1.0, specular=.50, base_color=(0.4735315, 0.4735315, 0.4735315, 1.0)),
}

def _shader_family(shader):
    s = shader.lower()
    if s.startswith('cockpit_'): s = s[len('cockpit_'):]
    if 'glass' in s: return 'glass'
    if 'chrome' in s: return 'chrome'
    if 'carbon_fiber' in s: return 'carbon_fiber'
    if 'brushed_metal' in s: return 'brushed_metal'
    if 'metal' in s: return 'metal'
    if 'cloth' in s or 'fabric' in s: return 'cloth'
    if 'stitching' in s: return 'leather'
    if 'leather' in s: return 'leather'
    if 'rubber' in s: return 'rubber'
    if 'dullplastic' in s: return 'dullplastic'
    if 'plastic' in s: return 'plastic'
    return None

def _apply_shader_preset(mat, bsdf, shader):
    """Apply editable shader presets, keeping Blender defaults for null values."""
    from .shader_preset_config import apply_preset
    family = _shader_family(shader)
    count = apply_preset(mat, bsdf, shader, family=family)
    mat['fh1_blender_preset'] = family or shader.lower().removeprefix('cockpit_')
    mat['fh1_preset_note'] = 'Editable preview approximation; not exported FH1 shader parameters'
    return bool(count)

def _alpha_coverage_on_mesh(mat, image, *, max_faces=500):
    """Check actual UV0-covered texels, not unrelated transparent atlas regions.

    This is an evidence-based *preview heuristic*, not a decoded Xenos render state.
    Returns (transparent_fraction, samples) or (None, 0) if no usable UV data.
    """
    import bpy
    if image is None or not image.has_data or not image.size[0] or not image.size[1]:
        return None, 0
    width, height = image.size[:]
    pixels = image.pixels
    candidates = []
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or not obj.data.uv_layers:
            continue
        mesh = obj.data
        slot_ids = {i for i, slot in enumerate(mesh.materials) if slot == mat}
        if not slot_ids:
            continue
        uv_layer = mesh.uv_layers.get('UV0') or mesh.uv_layers.active
        if uv_layer is None:
            continue
        faces = [poly for poly in mesh.polygons if poly.material_index in slot_ids and len(poly.loop_indices) >= 3]
        if not faces:
            continue
        stride = max(1, (len(faces) + max_faces - 1) // max_faces)
        for poly in faces[::stride]:
            indices = list(poly.loop_indices)
            uv = [uv_layer.data[i].uv for i in indices]
            # Vertices and interior barycentric positions sample cutouts that
            # a single polygon-centre test would miss.
            candidates.extend((float(v.x), float(v.y)) for v in uv[:4])
            a, b, c = uv[:3]
            for wa, wb, wc in ((1/3,1/3,1/3), (.6,.2,.2), (.2,.6,.2), (.2,.2,.6)):
                candidates.append((wa*a.x+wb*b.x+wc*c.x, wa*a.y+wb*b.y+wc*c.y))
    if not candidates:
        return None, 0
    transparent = 0
    for u, v in candidates:
        x = min(width-1, int((u % 1.0) * width))
        y = min(height-1, int((v % 1.0) * height))
        if pixels[(y * width + x) * 4 + 3] < 0.5:
            transparent += 1
    return transparent / len(candidates), len(candidates)


def _alpha_mode(mat, bsdf, image_node, sampler, shader):
    """Material UI override: diffuse alpha may drive either transparency or specular."""
    links = mat.node_tree.links
    if 'fh1_alpha_override' not in mat:
        mat['fh1_alpha_override'] = 'AUTO'
    choice = str(mat.get('fh1_alpha_override', 'AUTO')).upper()
    # Migrate v0.1.12's combined option to independent controls.
    if choice == 'TEXTURE_ALPHA_SPECULAR':
        mat['fh1_alpha_override'] = 'OPAQUE'
        mat['fh1_specular_override'] = 'TEXTURE_ALPHA'
        choice = 'OPAQUE'
    if choice not in ('AUTO', 'OPAQUE', 'CUTOUT', 'BLEND'):
        choice = 'AUTO'
    spec_choice = str(mat.get('fh1_specular_override', 'PRESET')).upper()
    family = shader.lower().removeprefix('cockpit_')
    if choice == 'AUTO':
        mode = 'BLEND' if family == 'glass' else 'OPAQUE'
        reason = 'conservative shader-family preview; GPU render state unverified'
    else:
        mode = choice
        reason = 'explicit user override'

    # Preview-only node routing. Rebuild our own links on each UI switch.
    spec = bsdf.inputs.get('Specular')
    alpha = bsdf.inputs.get('Alpha')
    nodes = mat.node_tree.nodes
    route_name = 'FH1 | Texture Alpha Specular'
    invert_name = 'FH1 | Diffuse Alpha Invert'
    mix_name = 'FH1 | Diffuse Alpha Detail Mix'
    for name in (route_name, invert_name, mix_name):
        node = nodes.get(name)
        if node is not None:
            nodes.remove(node)  # Also disconnects any previous override links.

    # Remove previous alpha preview links before wiring the selected mode.
    for link in tuple(alpha.links):
        links.remove(link)
    if spec is not None and 'fh1_specular_before_alpha' in mat and not spec.is_linked:
        spec.default_value = float(mat['fh1_specular_before_alpha'])

    if spec_choice in ('TEXTURE_ALPHA', 'ALPHA_INVERT_MIX') and image_node is not None:
        if spec is not None and 'fh1_specular_before_alpha' not in mat:
            mat['fh1_specular_before_alpha'] = float(spec.default_value)
        route = nodes.new('NodeReroute')
        route.name = route_name
        route.label = 'Diffuse alpha to specular'
        route.location = (-480, -330)
        links.new(image_node.outputs['Alpha'], route.inputs[0])
        if spec_choice == 'TEXTURE_ALPHA':
            if spec is not None:
                links.new(route.outputs[0], spec)
            mat['fh1_specular_method'] = 'diffuse alpha to Principled Specular (preview)'
        else:
            inv = nodes.new('ShaderNodeInvert')
            inv.name = invert_name
            inv.label = 'Invert diffuse alpha'
            inv.location = (-260, -300)
            inv.inputs['Fac'].default_value = 1.0
            links.new(route.outputs[0], inv.inputs['Color'])
            mix = nodes.new('ShaderNodeMixRGB')
            mix.name = mix_name
            mix.label = 'Diffuse alpha detail blend (50%)'
            mix.location = (-20, -300)
            mix.blend_type = 'MIX'
            mix.use_clamp = True
            mix.inputs[0].default_value = 0.5
            links.new(inv.outputs['Color'], mix.inputs[1])  # A: inverted alpha
            links.new(route.outputs[0], mix.inputs[2])  # B: original alpha
            links.new(mix.outputs['Color'], alpha)  # IMPORTANT: Alpha, NOT Specular
            mat['fh1_specular_method'] = 'diffuse alpha/inverse mix to Principled Alpha (preview)'
    else:
        mat['fh1_specular_method'] = 'shader preset'

    # The detail-blend mode owns the Alpha input, regardless of Alpha Mode.
    if spec_choice != 'ALPHA_INVERT_MIX' or image_node is None:
        if mode in ('CUTOUT', 'BLEND') and image_node is not None:
            links.new(image_node.outputs['Alpha'], alpha)
        else:
            alpha.default_value = (0.25 if family == 'glass' and choice == 'AUTO' else (0.5 if mode == 'BLEND' else 1.0))
    mat.blend_method = {'OPAQUE':'OPAQUE', 'CUTOUT':'HASHED', 'BLEND':'BLEND'}[mode]
    mat['fh1_alpha_source'] = ('diffuse alpha detail blend' if spec_choice == 'ALPHA_INVERT_MIX' and image_node is not None else (sampler if mode in ('CUTOUT','BLEND') and image_node else 'constant'))
    mat['fh1_alpha_decision'] = reason
    mat['fh1_alpha_mode'] = mode


def _color_sampler(sampler):
    s = sampler.lower()
    if any(x in s for x in ('normal','bump','damage','dirt','mask','specular','gloss','roughness','ao','occlusion')):
        return False
    return any(x in s for x in ('interior','diffuse','colour','color','albedo','paint','badge','leather','plastic','cloth','fabric','metal','carbon','grille','light','glass','stitch'))


def _data_sampler(sampler):
    s = sampler.lower()
    if 'normal' in s or 'bump' in s: return 'normal'
    if 'specular' in s or 'gloss' in s: return 'specular'
    if s.endswith('aosampler') or 'occlusion' in s: return 'ao'
    return None


def _math(nodes, name, op, x, y, constant=None):
    n=nodes.get(name) or nodes.new('ShaderNodeMath')
    n.name=name; n.label=name; n.operation=op; n.location=(x,y)
    if constant is not None: n.inputs[1].default_value=constant
    return n


def _connect_detail_maps(mat, bsdf, shader, images):
    """Preview-grade AO/specular routing. Named sampler evidence only."""
    nodes=mat.node_tree.nodes; links=mat.node_tree.links
    diffuse=next((n for s,n in images if _color_sampler(s)),None)
    ao=next((n for s,n in images if _data_sampler(s)=='ao'),None)
    spec=next((n for s,n in images if _data_sampler(s)=='specular'),None)
    if diffuse is not None and bsdf:
        if ao is not None:
            mix=nodes.get('FH1 | Diffuse x AO') or nodes.new('ShaderNodeMixRGB')
            mix.name='FH1 | Diffuse x AO'; mix.label='AO preview (multiply)'; mix.blend_type='MULTIPLY'
            mix.inputs[0].default_value=1.0; mix.location=(-130,320)
            links.new(diffuse.outputs['Color'],mix.inputs[1])
            links.new(ao.outputs['Color'],mix.inputs[2])
            links.new(mix.outputs['Color'],bsdf.inputs['Base Color'])
            mat['fh1_ao_method']='multiply preview (not verified Xenos equation)'
        else:
            links.new(diffuse.outputs['Color'],bsdf.inputs['Base Color'])
    if spec is not None and bsdf:
        # Do not interpret diffuse alpha as specular without a verified mapping.
        rgb=nodes.get('FH1 | Specular RGB') or nodes.new('ShaderNodeRGBToBW')
        rgb.name='FH1 | Specular RGB';rgb.location=(-120,-480)
        links.new(spec.outputs['Color'],rgb.inputs['Color'])
        strength=_math(nodes,'FH1 | Specular scale','MULTIPLY',100,-480,0.5)
        links.new(rgb.outputs['Val'],strength.inputs[0])
        if 'Specular' in bsdf.inputs:
            links.new(strength.outputs[0],bsdf.inputs['Specular'])
        mat['fh1_specular_method']='named specular sampler RGB luminance; preview scale 0.5'
    else:
        mat['fh1_specular_method']='existing FH1 shader-family preset'
    if bsdf:
        _alpha_mode(mat,bsdf,diffuse,images[0][0] if diffuse is not None else '',shader)

def _apply_preview_routing(mat, bsdf, shader, images, carbin_path, *, cockpit=False):
    """Connect observed FH1 vehicle atlas/sampler to Blender Base Color.

    Approximate preview routing only; never changes exported FH1 material data.
    """
    from .shader_preset_config import load_config, color_value
    if bsdf is None: return
    config=load_config().get('preview_routing',{})
    key=shader.lower().removeprefix('cockpit_').removeprefix('fh1_')
    rule=config.get('shaders',{}).get(key)
    if not rule: return
    nodes=mat.node_tree.nodes; links=mat.node_tree.links
    socket=bsdf.inputs.get('Base Color')
    selected=None
    # Secondary paint mirrors the primary paint preview, not a texture atlas.
    if 'copy_paint' in rule:
        primary=config.get('shaders',{}).get(rule['copy_paint'],{})
        rule=dict(primary)
        mat['fh1_preview_paint_copy']='body'
    if 'sampler' in rule and rule.get('texture') != 'lights':
        selected=next((node for name,node in images if name.lower()==rule['sampler'].lower()),None)
    if selected is None and rule.get('texture')=='lights':
        lod0=cockpit or 'lod0' in os.path.basename(carbin_path).lower()
        names=['lights_LOD0.xds','lights_s_LOD0.xds','lights.xds'] if lod0 else ['lights.xds','lights_LOD0.xds']
        car_dir=os.path.dirname(os.path.abspath(carbin_path))
        index=_file_index(car_dir)
        shared_dir=getattr(_apply_preview_routing,'shared_root',None)
        shared_index=_file_index(shared_dir) if shared_dir else {}
        for filename in names:
            path=_find(car_dir,filename,index=index) or _find(shared_dir,filename,index=shared_index)
            if path:
                try:
                    image,_=xds_textures.load_xds_image(path)
                    selected=nodes.get('FH1 | Preview Lights') or nodes.new('ShaderNodeTexImage')
                    selected.name='FH1 | Preview Lights';selected.label=filename
                    selected.image=image;selected.location=(-750,-600)
                    break
                except Exception as exc:
                    print('[FH1 preview] Light decode error %s: %s'%(path,exc))
    if selected is None and 'atlas' in rule:
        stem=rule['atlas']
        lod0=cockpit or 'lod0' in os.path.basename(carbin_path).lower()
        suffixes=[stem+'_LOD0',stem] if lod0 else [stem,stem+'_LOD0']
        car_dir=os.path.dirname(os.path.abspath(carbin_path))
        index=_file_index(car_dir)
        for suffix in suffixes:
            path=_find(car_dir,suffix+'.xds',index=index)
            if not path: continue
            try:
                image,_=xds_textures.load_xds_image(path)
                selected=nodes.get('FH1 | Preview Atlas') or nodes.new('ShaderNodeTexImage')
                selected.name='FH1 | Preview Atlas'
                selected.label=suffix+' (vehicle atlas)'
                selected.image=image
                selected.location=(-750,-600)
                break
            except Exception as exc:
                print('[FH1 preview] Atlas decode error %s: %s'%(path,exc))
    if selected is not None and socket is not None:
        for link in tuple(socket.links): links.remove(link)
        links.new(selected.outputs['Color'],socket)
        mat['fh1_preview_source']=selected.image.name if selected.image else 'image'
    elif 'color' in rule and socket is not None:
        for link in tuple(socket.links): links.remove(link)
        socket.default_value=color_value({'hex':rule['color']})
        mat['fh1_preview_source']='constant'
    elif socket is not None and not socket.is_linked:
        socket.default_value=color_value({'hex':config.get('fallback_color','#B33C9A')})
        mat['fh1_preview_source']='missing texture (diagnostic)'
        print('[FH1 preview] Missing texture for %s (%s)'%(mat.name,key))
    for field, setting in (('Metallic','metallic'),('Roughness','roughness')):
        inp=bsdf.inputs.get(field)
        if inp is not None and setting in rule:
            for link in tuple(inp.links): links.remove(link)
            inp.default_value=float(rule[setting])
    if 'alpha' in rule:
        inp=bsdf.inputs.get('Alpha')
        if inp is not None:
            for link in tuple(inp.links): links.remove(link)
            inp.default_value=float(rule['alpha'])
        try: mat.blend_method='BLEND'
        except Exception: pass
    if key == 'black_glass':
        # Opaque preview: ignore atlas/image alpha, while preserving Base Color routing.
        alpha_input=bsdf.inputs.get('Alpha')
        if alpha_input is not None:
            for link in tuple(alpha_input.links): links.remove(link)
            alpha_input.default_value=1.0
        mat.blend_method='OPAQUE'
        try: mat.use_nodes=True
        except Exception: pass
        mat['fh1_alpha_mode']='OPAQUE'
        mat['fh1_alpha_source']='constant'
    mat['fh1_preview_rule']=key


def apply_materials(carbin_path, shared_root, *, cockpit=False, superlod=False):
    """Sampler-aware Blender preview materials, with inspectable routing."""
    import bpy
    mapping=resolve_materials(carbin_path,shared_root,cockpit=cockpit,superlod=superlod)
    _apply_preview_routing.shared_root=shared_root
    cache={}; linked=0; missing=[]; assigned=0; decoded=0; reused=0
    started=time.perf_counter()
    for name,(shader,bindings) in mapping.items():
        mat=bpy.data.materials.get(name)
        if mat is None: continue
        mat.use_nodes=True
        nodes=mat.node_tree.nodes;links=mat.node_tree.links
        bsdf=next((n for n in nodes if n.type=='BSDF_PRINCIPLED'),None)
        if bsdf is None:
            bsdf=nodes.new('ShaderNodeBsdfPrincipled')
            bsdf.name='FH1 | Principled Preview'; bsdf.location=(0,0)
        output=next((n for n in nodes if n.type=='OUTPUT_MATERIAL'),None)
        if output is None:
            output=nodes.new('ShaderNodeOutputMaterial'); output.location=(350,0)
        if not output.inputs['Surface'].is_linked:
            links.new(bsdf.outputs['BSDF'],output.inputs['Surface'])
        # Window glass uses the same Blender preset as the configured glass shader.
        preview_shader = 'glass' if shader.lower().removeprefix('cockpit_').removeprefix('fh1_') == 'window' else shader
        _apply_shader_preset(mat,bsdf,preview_shader)
        mat['fh1_resolved_shader']=shader
        mat['fh1_resolved_samplers']='; '.join(s for s,_ in bindings)
        mat['fh1_material_accuracy']='sampler-based preview; original blend states and specular equations unverified'
        if shader.lower().removeprefix('cockpit_') in ('solid','badge'):
            mat.use_backface_culling=True
        images=[]
        for index,(sampler,path) in enumerate(bindings):
            try:
                if path not in cache:
                    image,was_decoded=xds_textures.load_xds_image(path)
                    cache[path]=image;decoded+=int(was_decoded);reused+=int(not was_decoded)
                node=nodes.get('FH1 | '+sampler) or nodes.new('ShaderNodeTexImage')
                node.name='FH1 | '+sampler;node.label=sampler
                node.image=cache[path];node.location=(-750,280-index*290)
                assigned+=1;images.append((sampler,node))
                purpose=_data_sampler(sampler)
                if purpose is not None:
                    try: node.image.colorspace_settings.name='Non-Color'
                    except Exception: pass
                fmt=cache[path].get('fh1_xds_format')
                is_stitch=sampler=='CockpitStitchingNormalSampler' and fmt=='BC3'
                is_bc5=fmt=='BC5' and 'NormalSampler' in sampler
                if bsdf and (is_stitch or is_bc5):
                    normal=nodes.get('FH1 Normal | '+sampler) or nodes.new('ShaderNodeNormalMap')
                    normal.name='FH1 Normal | '+sampler;normal.space='TANGENT'
                    normal.location=(-280,250-index*290)
                    if cockpit:
                        uv=nodes.get('FH1 UV1 | '+sampler) or nodes.new('ShaderNodeUVMap')
                        uv.name='FH1 UV1 | '+sampler;uv.uv_map='UV1'
                        uv.location=(-970,250-index*290)
                        links.new(uv.outputs['UV'],node.inputs['Vector'])
                    links.new(node.outputs['Color'],normal.inputs['Color'])
                    links.new(normal.outputs['Normal'],bsdf.inputs['Normal'])
                    linked+=1
            except Exception as exc:
                missing.append((name,sampler,str(exc)))
        if bsdf:
            _connect_detail_maps(mat,bsdf,shader,images)
            if _shader_family(shader) == 'glass' or shader.lower().removeprefix('cockpit_').removeprefix('fh1_') == 'window':
                for link in tuple(bsdf.inputs['Base Color'].links):
                    links.remove(link)
                pass  # Glass preview appearance is configured in shader_presets.json
            _apply_preview_routing(mat,bsdf,shader,images,carbin_path,cockpit=cockpit)
            if shader.lower().removeprefix('cockpit_').removeprefix('fh1_') == 'window':
                # Glass appearance comes from the JSON glass preset, not an atlas.
                for link in tuple(bsdf.inputs['Base Color'].links): links.remove(link)
                mat['fh1_preview_source'] = 'glass JSON preset'
            linked+=int(bool(images))
    # Dev42: apply Blender-only JSON shader templates AFTER native sampler resolution.
    try:
        from . import material_templates
        rebuilt=0
        # Resolve template-only images from the CURRENT car, not the reference S2000.
        # Some taillight shaders use DamageLights in the Blender template even
        # when that image is absent from their native sampler bindings.
        lod0_import = cockpit or 'lod0' in os.path.basename(carbin_path).lower()
        car_root = os.path.dirname(os.path.abspath(carbin_path))
        car_index = _file_index(car_root)
        shared_index = _file_index(shared_root)
        vehicle_images = {}
        for family in ('lights', 'damagelights', 'damage', 'nodamage', 'glass_texture'):
            variants = (family + '_LOD0.xds', family + '.xds') if lod0_import else (family + '.xds', family + '_LOD0.xds')
            for filename in variants:
                path = _find(car_root, filename, index=car_index) or _find(shared_root, filename, index=shared_index)
                if not path: continue
                try:
                    if path not in cache:
                        image, was_decoded = xds_textures.load_xds_image(path)
                        cache[path] = image
                        decoded += int(was_decoded)
                        reused += int(not was_decoded)
                    vehicle_images[family] = cache[path]
                    print('[FH1 Templates] %s -> %s' % (family, os.path.basename(path)))
                    break
                except Exception as exc:
                    print('[FH1 Templates] Could not load %s: %s' % (path, exc))
        for name in mapping:
            mat=bpy.data.materials.get(name)
            if mat and material_templates.apply_template(mat, lod0=lod0_import, vehicle_images=vehicle_images):rebuilt+=1
        print('[FH1 Templates] Rebuilt %d materials from shader_presets.json' % rebuilt)
    except Exception as exc:
        import traceback;traceback.print_exc()
        missing.append(('template reconstruction','',str(exc)))
    # Dev78: Body paint visual-damage ramp, after template reconstruction.
    # Keep the original damage sampler and its Fac/Color connections intact.
    for name, (shader, bindings) in mapping.items():
        family = shader.lower().removeprefix('cockpit_').removeprefix('fh1_')
        if family != 'body':
            continue
        mat = bpy.data.materials.get(name)
        if mat is None or not mat.use_nodes:
            continue
        ramp = mat.node_tree.nodes.get('Color Ramp')
        if ramp is None or ramp.type != 'VALTORGB':
            continue
        elements = ramp.color_ramp.elements
        if len(elements) >= 2:
            elements[0].position = 0.0
            elements[0].color = (0.0, 0.0, 0.0, 1.0)
            elements[1].position = 0.125
            elements[1].color = (1.0, 1.0, 1.0, 1.0)
            ramp.color_ramp.interpolation = 'LINEAR'
    # Dev73: Badge previews use the same image alpha as their diffuse/atlas
    # texture, including after JSON templates rebuild the material graph.
    # Leave the source node present when the image is missing.
    for name, (shader, bindings) in mapping.items():
        family = shader.lower().removeprefix('cockpit_').removeprefix('fh1_')
        if family != 'badge':
            continue
        mat = bpy.data.materials.get(name)
        if not mat or not mat.use_nodes:
            continue
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is None or bsdf.inputs.get('Alpha') is None:
            continue
        alpha = bsdf.inputs['Alpha']
        # Prefer the texture driving Base Color (the same source as the badge
        # diffuse preview), otherwise a resolved badge/atlas image.
        source = None
        base = bsdf.inputs.get('Base Color')
        if base and base.is_linked:
            upstream = base.links[0].from_node
            if upstream.type == 'TEX_IMAGE' and upstream.image:
                source = upstream
        if source is None:
            candidates = [n for n in nodes if n.type == 'TEX_IMAGE' and n.image]
            source = next((n for n in candidates if 'badge' in (n.name + ' ' + n.label).lower()), None)
            if source is None:
                source = next((n for n in candidates if 'atlas' in (n.name + ' ' + n.label).lower()), None)
            if source is None and len(candidates) == 1:
                source = candidates[0]
        for link in tuple(alpha.links):
            links.remove(link)
        if source is not None:
            links.new(source.outputs['Alpha'], alpha)
            mat['fh1_alpha_source'] = source.name + ' alpha'
        else:
            alpha.default_value = 1.0
            mat['fh1_alpha_source'] = 'constant (badge texture missing)'
        mat.blend_method = 'BLEND'
        mat['fh1_alpha_mode'] = 'BLEND'
    # Dev74: The FH1 bottom shader previews the vehicle's no-damage atlas.
    # Run after JSON template reconstruction so its links are not overwritten.
    for name, (shader, bindings) in mapping.items():
        family = shader.lower().removeprefix('cockpit_').removeprefix('fh1_')
        if family != 'bottom':
            continue
        mat = bpy.data.materials.get(name)
        if not mat or not mat.use_nodes:
            continue
        nodes, links = mat.node_tree.nodes, mat.node_tree.links
        bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is None:
            continue
        spec = bsdf.inputs.get('Specular')
        if spec is not None:
            for link in tuple(spec.links):
                links.remove(link)
            spec.default_value = 0.0
        atlas = vehicle_images.get('nodamage') if 'vehicle_images' in locals() else None
        base = bsdf.inputs.get('Base Color')
        if base is None:
            continue
        for link in tuple(base.links):
            links.remove(link)
        tex = nodes.get('FH1 Bottom | NoDamage')
        if tex is None or tex.type != 'TEX_IMAGE':
            tex = nodes.new('ShaderNodeTexImage')
            tex.name = 'FH1 Bottom | NoDamage'
        tex.label = 'Vehicle NoDamage (LOD-aware)'
        tex.location = (bsdf.location.x - 340, bsdf.location.y + 80)
        tex.image = atlas
        if atlas is not None:
            links.new(tex.outputs['Color'], base)
            mat['fh1_preview_source'] = atlas.name
        else:
            mat['fh1_preview_source'] = 'nodamage texture missing (unlinked sampler)'
        mat['fh1_specular_method'] = 'bottom preset: constant 0'
    # Dev75: S2000 LOD fullbeam/oldhead/hidhead use the Lights atlas, not NoDamage.
    # Applied after template reconstruction, matching the bottom shader override.
    if not lod0_import:
        for name, (shader, bindings) in mapping.items():
            family = shader.lower().removeprefix('cockpit_').removeprefix('fh1_')
            if family not in ('fullbeam', 'oldhead', 'hidhead'):
                continue
            mat = bpy.data.materials.get(name)
            if not mat or not mat.use_nodes:
                continue
            nodes, links = mat.node_tree.nodes, mat.node_tree.links
            bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
            if bsdf is None:
                continue
            atlas = vehicle_images.get('lights')
            tex = nodes.get('FH1 Headlight | Lights')
            if tex is None or tex.type != 'TEX_IMAGE':
                tex = nodes.new('ShaderNodeTexImage')
                tex.name = 'FH1 Headlight | Lights'
            tex.label = 'Vehicle Lights (LOD1-5)'
            tex.location = (bsdf.location.x - 340, bsdf.location.y + 80)
            tex.image = atlas
            base = bsdf.inputs.get('Base Color')
            if base is not None:
                for link in tuple(base.links):
                    links.remove(link)
                if atlas is not None:
                    links.new(tex.outputs['Color'], base)
            if family in ('oldhead', 'hidhead'):
                spec = bsdf.inputs.get('Specular') or bsdf.inputs.get('Specular IOR Level')
                if spec is not None:
                    for link in tuple(spec.links):
                        links.remove(link)
                    spec.default_value = 0.5
                    if family == 'hidhead' and atlas is not None:
                        links.new(tex.outputs['Alpha'], spec)
                mat['fh1_specular_method'] = ('Lights texture alpha' if atlas is not None else 'fallback 0.5') if family == 'hidhead' else 'constant 0.5 (unlinked)'
            mat['fh1_preview_source'] = atlas.name if atlas is not None else 'lights texture missing (unlinked sampler)'
    # Dev77: fullbeam, oldhead and hidhead share indicator_right's Light Damage
    # preview system. Keep each shader's existing undamaged Lights/Specular setup.
    # The damaged branch uses DamageLights; both are mixed by the SAME shared
    # Light Damage group as indicator_right (one global FH1 Shader Control).
    if not lod0_import:
        try:
            from . import material_templates
            reference = material_templates.templates().get('fh1_indicator_right', {})
            group_spec = next((n for n in reference.get('node_tree', {}).get('nodes', [])
                               if n.get('type') == 'GROUP' and
                               (n.get('group_definition') or {}).get('name') == 'Light Damage'), None)
            damage_group = None
            if group_spec:
                damage_group = material_templates._shared_group(
                    group_spec['group_definition'], group_spec.get('inputs', []),
                    group_spec.get('outputs', []), {})
            for name, (shader, bindings) in mapping.items():
                family = shader.lower().removeprefix('cockpit_').removeprefix('fh1_')
                if family not in ('fullbeam', 'oldhead', 'hidhead'):
                    continue
                mat = bpy.data.materials.get(name)
                if not mat or not mat.use_nodes or damage_group is None:
                    continue
                nodes, links = mat.node_tree.nodes, mat.node_tree.links
                original = nodes.get('Principled BSDF')
                if original is None or original.type != 'BSDF_PRINCIPLED':
                    original = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
                output = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
                if output is None:
                    output = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
                if original is None or output is None:
                    continue
                def node(kind, name, label, loc):
                    n = nodes.get(name)
                    if n is not None and n.bl_idname != kind:
                        nodes.remove(n)
                        n = None
                    if n is None:
                        n = nodes.new(kind)
                        n.name = name
                    n.label = label
                    n.location = loc
                    return n
                x, y = original.location
                damage_tex = node('ShaderNodeTexImage', 'FH1 Headlight | DamageLights',
                                  'Vehicle DamageLights (LOD1-5)', (x-400, y-440))
                damage_tex.image = vehicle_images.get('damagelights')
                damaged = node('ShaderNodeBsdfPrincipled', 'FH1 Headlight | Damaged BSDF',
                               'Damaged light', (x, y-380))
                # Mirror the undamaged PBR defaults, while retaining its special
                # Lights alpha -> Specular routing exclusively on that branch.
                for socket in damaged.inputs:
                    other = original.inputs.get(socket.name)
                    if other is None or socket.is_linked or other.is_linked:
                        continue
                    try: socket.default_value = other.default_value
                    except (AttributeError, TypeError, ValueError): pass
                base = damaged.inputs.get('Base Color')
                if base is not None:
                    for link in tuple(base.links): links.remove(link)
                    if damage_tex.image is not None:
                        links.new(damage_tex.outputs['Color'], base)
                mix = node('ShaderNodeMixShader', 'FH1 Headlight | Damage Mix',
                           'Undamaged / Damaged', (x+380, y-80))
                control = node('ShaderNodeGroup', 'FH1 Headlight | Light Damage',
                               'Light Damage (shared)', (x-20, y-650))
                control.node_tree = damage_group
                links.new(control.outputs['Value'], mix.inputs[0])
                links.new(original.outputs['BSDF'], mix.inputs[1])
                links.new(damaged.outputs['BSDF'], mix.inputs[2])
                links.new(mix.outputs[0], output.inputs['Surface'])
                mat['fh1_light_damage_preview'] = 'shared indicator_right Light Damage'
                mat['fh1_damage_atlas'] = ('damagelights' if damage_tex.image else 'missing')
        except Exception as exc:
            import traceback; traceback.print_exc()
            missing.append(('headlight damage preview', '', str(exc)))
    # Wheel-only preview materials, applied AFTER JSON template reconstruction.
    try:
        from . import wheel_materials
        wheel_materials.apply_wheel_materials(carbin_path, shared_root, cache)
    except Exception as exc:
        import traceback; traceback.print_exc()
        missing.append(('wheel preview','',str(exc)))
    print('[FH1 Materials] %.2fs, %d newly decoded, %d reused' % (time.perf_counter()-started,decoded,reused))
    return {'materials_resolved':len(mapping),'image_nodes':assigned,'diffuse_links':linked,
            'unique_images':len(cache),'decoded':decoded,'reused':reused,'errors':missing}
