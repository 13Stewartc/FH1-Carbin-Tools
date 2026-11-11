"""Dev42 Blender-only shader template reconstruction (Blender 3.6)."""
import bpy, json, os, re, hashlib

_CACHE = None

def templates():
    """The integrated shader_presets.json is the only runtime template source."""
    global _CACHE
    if _CACHE is None:
        path = os.path.join(os.path.dirname(__file__), 'shader_presets.json')
        with open(path, 'r', encoding='utf-8') as f:
            _CACHE = json.load(f).get('material_templates', {})
        print('[FH1 Templates] Loaded %d embedded material templates' % len(_CACHE))
    return _CACHE

def _set(obj, attr, value):
    if value is None or isinstance(value, dict): return
    try: setattr(obj, attr, value)
    except (AttributeError, TypeError, ValueError): pass

def _socket(sockets, spec):
    idx=spec.get('index')
    if isinstance(idx,int) and 0 <= idx < len(sockets):
        s=sockets[idx]
        if s.name == spec.get('name') or s.identifier == spec.get('identifier'): return s
    return sockets.get(spec.get('name',''))

def _shared_group(definition, inputs, outputs, groups):
    """Reuse matching group definitions across every imported FH1 material."""
    key = hashlib.sha256(json.dumps(definition, sort_keys=True).encode('utf-8')).hexdigest()
    if key in groups:
        return groups[key]
    for candidate in bpy.data.node_groups:
        if candidate.bl_idname == 'ShaderNodeTree' and candidate.get('fh1_template_signature') == key:
            groups[key] = candidate
            return candidate
    base = definition.get('name') or 'FH1 Template Group'
    ng = bpy.data.node_groups.new(base, 'ShaderNodeTree')
    ng['fh1_template_signature'] = key
    ng['fh1_template_group'] = True
    groups[key] = ng  # cache before recursing
    for spec, collection in ((inputs, ng.inputs), (outputs, ng.outputs)):
        for socket in spec:
            try:
                collection.new(socket.get('type', 'NodeSocketFloat'), socket['name'])
            except Exception as exc:
                print('[FH1 Template] Group socket skipped:', base, exc)
    _tree(ng, definition, groups)
    return ng

def _tree(tree, spec, groups):
    tree.nodes.clear()
    made={}
    for d in spec.get('nodes',[]):
        try: n=tree.nodes.new(d['bl_idname'])
        except Exception as exc:
            print('[FH1 Template] Node unavailable:',d.get('name'),exc);continue
        n.name=d['name'];n.label=d.get('label','')
        for k,v in d.get('properties',{}).items():
            if k in ('data_type','mode','operation','blend_type','factor_mode','feature','distance','noise_dimensions','voronoi_dimensions','space','vector_type','distribution','subsurface_method','interpolation','extension','projection','clamp_factor','clamp_result','invert','is_active_output','target'):
                _set(n,k,v)
        if n.type=='GROUP':
            g=d.get('group_definition')
            if g:
                n.node_tree = _shared_group(g, d.get('inputs', []), d.get('outputs', []), groups)
        if n.type=='VALTORGB' and 'color_ramp' in d:
            ramp=d['color_ramp'];_set(n.color_ramp,'interpolation',ramp.get('interpolation'))
            els=ramp.get('elements',[])
            if els:
                while len(n.color_ramp.elements)>1: n.color_ramp.elements.remove(n.color_ramp.elements[-1])
                for j,e in enumerate(els):
                    el=n.color_ramp.elements[0] if j==0 else n.color_ramp.elements.new(e['position'])
                    el.position=e['position'];el.color=e['color']
        for s in d.get('inputs',[]):
            sock=_socket(n.inputs,s)
            if sock and 'default_value' in s: _set(sock,'default_value',s['default_value'])
        for s in d.get('outputs',[]):
            sock=_socket(n.outputs,s)
            if sock and 'default_value' in s: _set(sock,'default_value',s['default_value'])
        for k in ('location','width','hide','mute','use_custom_color','color'):
            if k in d: _set(n,k,d[k])
        if n.type=='TEX_IMAGE':
            img=d.get('image') or {}
            # Images are rebound from current imported vehicle, not S2K temporary paths.
            n['fh1_template_image_name']=img.get('name','')
        made[d['name']]=n
    for d in spec.get('nodes',[]):
        n=made.get(d['name']);parent=made.get(d.get('parent'))
        if n and parent: n.parent=parent
    for link in spec.get('links',[]):
        a=made.get(link['from_node']);b=made.get(link['to_node'])
        if not a or not b:continue
        def get_sock(coll,name,idx):
            if isinstance(idx,int) and idx<len(coll) and coll[idx].name==name:return coll[idx]
            return coll.get(name)
        so=get_sock(a.outputs,link['from_socket'],link.get('from_socket_index'))
        si=get_sock(b.inputs,link['to_socket'],link.get('to_socket_index'))
        if so and si:
            try:tree.links.new(so,si)
            except Exception as exc: print('[FH1 Template] Link skipped:',exc)

def _shader_key(mat):
    name=str(mat.get('fh1_resolved_shader') or mat.get('fh1_shader_name') or mat.name)
    name=re.sub(r'^(cockpit_|fh1_)','',name,flags=re.I)
    return 'fh1_'+name.lower()

def _find_image(d, existing, lod0=False, vehicle_images=None):
    """Bind ONLY vehicle-resolved images; never copy reference-car images."""
    name = d.get('name','').lower()
    label = d.get('label','').lower()
    saved = (d.get('image') or {}).get('name','').lower()
    tokens = ' '.join((name,label,saved))
    # Explicit sampler identity beats the original image's name.
    families = (
        ('damagelightssampler','damagelights'),
        ('vehicledamagesampler','damage'),
        ('lightssampler','lights'),
        ('damageglasssampler','glass_texture'),
        ('dirtmasksampler','dirt_mask'),
        ('dirtsampler','dirt_pattern'),
    )
    family = None
    for marker,stem in families:
        if marker in tokens:
            family=stem;break
    if family is None:
        if 'preview lights' in name:
            family='damagelights' if 'damage' in tokens else 'lights'
        elif 'preview atlas' in name or 'nodamage' in tokens:
            family='nodamage'
        elif 'damagelights' in tokens: family='damagelights'
        elif 'lights_lod0' in tokens or 'lights.xds' in tokens:family='lights'
        elif 'damage_lod0' in tokens or 'damage.xds' in tokens:family='damage'
    # Existing images are captured before graph reconstruction.
    if family and vehicle_images and family in vehicle_images:
        return vehicle_images[family]
    if family:
        preferred = (family+'_lod0',family) if lod0 else (family,family+'_lod0')
        for stem in preferred:
            for img in existing.values():
                basename = os.path.basename(bpy.path.abspath(img.filepath)).lower()
                image_name = img.name.lower()
                # Check full filename stem boundaries to avoid damage matching damagelights.
                for candidate in (basename,image_name):
                    if re.search(r'(?<![a-z])'+re.escape(stem)+r'(?![a-z0-9])',candidate):
                        if stem==family and re.search(re.escape(family)+r'_lod0',candidate):continue
                        return img
    for key in (name,label):
        if key and key in existing:return existing[key]
    return None

def apply_template(mat, lod0=False, vehicle_images=None):
    d=templates().get(_shader_key(mat))
    if not d:return False
    if not mat.use_nodes:mat.use_nodes=True
    existing={}
    for n in mat.node_tree.nodes:
        if n.type=='TEX_IMAGE' and n.image:
            existing[n.name.lower()]=n.image
            existing[n.label.lower()]=n.image
    groups={}
    _tree(mat.node_tree,d['node_tree'],groups)
    for ndata in d['node_tree']['nodes']:
        if ndata.get('type')!='TEX_IMAGE':continue
        n=mat.node_tree.nodes.get(ndata['name'])
        if not n:continue
        img=_find_image(ndata,existing,lod0=lod0,vehicle_images=vehicle_images)
        if img:n.image=img
    for k,v in d.get('properties',{}).items():
        if k in ('blend_method','use_backface_culling','show_transparent_back','alpha_threshold','use_screen_refraction','refraction_depth'):
            _set(mat,k,v)
    mat['fh1_template_applied']=True
    print('[FH1 Templates] %s: %d nodes, %d links, %d images' % (mat.name,len(mat.node_tree.nodes),len(mat.node_tree.links),sum(bool(n.image) for n in mat.node_tree.nodes if n.type=='TEX_IMAGE')))
    mat['fh1_template_name']=d['name']
    return True

def _markers(tree, path='', seen=None):
    if seen is None:seen=set()
    if not tree or tree.as_pointer() in seen:return
    seen.add(tree.as_pointer())
    for n in tree.nodes:
        if n.name.strip().lower()=='show in n menu' and n.type in ('RGB','VALUE'):
            yield (path or 'Material',n)
        if n.type=='GROUP' and n.node_tree:
            yield from _markers(n.node_tree,(path+' / ' if path else '')+n.node_tree.name,seen)

def _scene_controls():
    """Find controls in all FH1 template materials, regardless of selection."""
    seen_groups = set()
    for mat in bpy.data.materials:
        if not mat.use_nodes or not mat.get('fh1_template_applied'):
            continue
        for node in mat.node_tree.nodes:
            if node.type != 'GROUP' or not node.node_tree:
                continue
            group = node.node_tree
            key = group.as_pointer()
            if key in seen_groups:
                continue
            seen_groups.add(key)
            controls = list(_markers(group))
            if controls:
                yield group.name, controls

class FH1CARBIN_PT_template_controls(bpy.types.Panel):
    bl_label = 'FH1 Shader Controls'
    bl_idname = 'FH1CARBIN_PT_template_controls'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'FH1 CARBIN'

    @classmethod
    def poll(cls, context):
        return True

    def draw(self, context):
        layout = self.layout
        found = False
        controls_by_group = list(_scene_controls())
        priority = {'Car Paint [Primary]': 0, 'Visual Damage (Paint)': 1, 'Light Damage': 2, 'Glass Damage': 3}
        controls_by_group.sort(key=lambda item: (priority.get(item[0], 10), item[0]))
        for group_name, controls in controls_by_group:
            found = True
            box = layout.box()
            box.label(text=group_name, icon='NODETREE')
            for path, node in controls:
                if not node.outputs:
                    continue
                # Edit the real shared group node; all referencing materials update.
                box.prop(node.outputs[0], 'default_value',
                         text='Color' if node.type == 'RGB' else 'Value')
        # One shared wheel blur control for all material instances.
        if any(mat.get('fh1_wheel_blur_preview') for mat in bpy.data.materials):
            found = True
            box = layout.box()
            box.label(text='Wheel Blur Layer', icon='TEXTURE')
            box.prop(context.scene, 'fh1_wheel_blur_disabled', text='Disable')
            row = box.row()
            row.enabled = not context.scene.fh1_wheel_blur_disabled
            row.prop(context.scene, 'fh1_wheel_blur_frame', text='Frame')
        if not found:
            layout.label(text='No FH1 template controls loaded', icon='INFO')
