"""v5.0.4: cached native shader availability with vertical browser and optional search."""
import os
from . import material_resolver, shader_preset_config
import bpy
from bpy.props import StringProperty, EnumProperty, BoolProperty, CollectionProperty, IntProperty

_CACHE = {'catalogue': {}, 'candidates': {}, 'message': 'Click Refresh Availability to scan the scene.', 'unresolved': [], 'fxobj': {}, 'fxobj_note': '', 'warnings': {}, 'occupied': set()}

def _scene_materials():
    result = set()
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        for slot in obj.material_slots:
            if slot.material:
                result.add(slot.material)
    return result

def _name(mat):
    return str(mat.get('fh1_custom_native_shader') or mat.get('fh1_shader_name') or mat.get('fh1_resolved_shader') or (mat.name[4:] if mat.name.startswith('FH1_') else '')).strip()

def _dependencies(name, normal, tex):
    node = normal.find(name)
    if node is None:
        return None
    deps = set()
    sampler_count = 0
    for child in node:
        if not child.tag.endswith('Sampler'):
            continue
        sampler_count += 1
        mapping = tex.find(child.tag)
        if mapping is None:
            return None
        options = mapping.findall('Texture')
        if not options:
            return None
        for option in options:
            filename = option.get('value', '').strip()
            path = option.get('searchPath', '').strip()
            if not filename or not path or '%' not in path:
                return None
            deps.add((path.lower(), filename.lower()))
    return deps if sampler_count and deps else None

def _config_root(context):
    scene = context.scene
    if scene.fh1_shader_config_override:
        return bpy.path.abspath(scene.fh1_shader_catalog_root)
    addon = context.preferences.addons.get(__package__)
    if addon and getattr(addon.preferences, 'shared_texture_folder', ''):
        return bpy.path.abspath(addon.preferences.shared_texture_folder)
    return ''

def _fxobj_catalogue(folder):
    """Recursively locate native vehicle libraries; report all other FXOBJ resources.

    This checks exact NUL-terminated name presence, not sampler/pass compatibility.
    If multiple copies of a required library exist, check all copies rather than
    silently choosing the last path returned by os.walk.
    """
    if not folder or not os.path.isdir(folder):
        return {}, 'Game Shader Folder not configured or inaccessible'
    required = ('shaders_v16.fxobj', 'shaders_slod_v16.fxobj')
    found = {key: [] for key in required}
    total = 0
    other = 0
    unreadable = []
    for base, dirs, names in os.walk(folder, onerror=lambda e: unreadable.append(str(e))):
        dirs.sort(key=str.lower)
        for filename in sorted(names, key=str.lower):
            if not filename.lower().endswith('.fxobj'):
                continue
            total += 1
            key = filename.lower()
            if key in found:
                found[key].append(os.path.join(base, filename))
            else:
                other += 1
    missing = [key for key, paths in found.items() if not paths]
    result = {}
    for key, paths in found.items():
        blobs = []
        for path in paths:
            try:
                with open(path, 'rb') as stream:
                    blobs.append(stream.read())
            except OSError as exc:
                unreadable.append('%s: %s' % (path, exc))
        if blobs:
            result[key] = blobs
    summary = '%d FXOBJ found recursively; %d vehicle library file(s); %d other' % (
        total, sum(map(len, found.values())), other)
    if missing:
        summary += '; missing ' + ', '.join(missing)
    if unreadable:
        summary += '; %d unreadable path(s)' % len(unreadable)
    summary += '; name presence only (bindings/LOD unverified)'
    return result, summary

def _native_status(name):
    libraries = _CACHE['fxobj']
    if not libraries:
        return 'UNVERIFIED (no native libraries)'
    token = name.encode('ascii', errors='ignore') + b'\x00'
    present = [key for key, blobs in libraries.items() if any(token in blob for blob in blobs)]
    if len(present) == 2:
        return 'NAME IN BOTH (bindings unverified)'
    if present:
        return 'NAME IN ' + present[0] + ' ONLY'
    return 'NAME NOT FOUND IN NATIVE LIBRARIES'

def refresh(root, shader_folder=""):
    _CACHE['fxobj'], _CACHE['fxobj_note'] = _fxobj_catalogue(shader_folder)
    _CACHE['catalogue'] = {}
    _CACHE['candidates'] = {}
    _CACHE['unresolved'] = []
    _CACHE['warnings'] = {}
    normal_path = material_resolver._find(root, 'ShaderSettings/Normal.xml')
    tex_path = material_resolver._find(root, 'textures.xml')
    if not normal_path or not tex_path:
        _CACHE['message'] = 'Select an extracted shared folder containing ShaderSettings/Normal.xml and textures.xml.'
        return 0
    normal = material_resolver._xml(normal_path)
    tex = material_resolver._xml(tex_path)
    mats = _scene_materials()
    if not mats:
        _CACHE['message'] = 'No mesh materials in scene. Import the vehicle LODs first.'
        return 0
    occupied = {_name(m).lower() for m in mats if _name(m)}
    _CACHE['occupied'] = occupied
    used_deps = set()
    unknown = []
    for name in occupied:
        deps = _dependencies(name, normal, tex)
        if deps is None:
            unknown.append(name)
        else:
            used_deps.update(deps)
    # Discovery and safety certification are separate. Unknown occupied mappings
    # cannot certify a candidate, but should not hide discoverable options.
    _CACHE['unresolved'] = sorted(unknown)
    # Do NOT reserve dependencies between candidate shaders. Multiple native
    # techniques legitimately share dirt masks, environment maps and other
    # engine resources; a collision is diagnostic, not a discovery veto.
    # badge2, for example, uses both vehicle-local Badge2Sampler and the
    # shared DirtMaskSampler. Excluding shared dependencies hid it entirely.
    for node in normal:
        name = node.tag
        deps = _dependencies(name, normal, tex)
        if deps is None:
            continue
        collisions = deps.intersection(used_deps)
        _CACHE['catalogue'][name] = deps
        if name.lower() in occupied:
            continue
        _CACHE['candidates'][name] = deps
        if collisions:
            _CACHE['warnings'][name] = 'Shares existing texture(s): ' + ', '.join(
                sorted('%s/%s' % pair for pair in collisions))
    if unknown:
        _CACHE['message'] = ('%d provisional candidate(s), %d occupied; %d unresolved occupied shader mappings '
                             '(first: %s). Provisional assignment permitted for testing; bindings remain unverified.' %
                             (len(_CACHE['candidates']), len(occupied), len(unknown), ', '.join(sorted(unknown)[:5])))
    else:
        _CACHE['message'] = ('%d candidate(s), %d occupied (shared dependencies shown, not excluded). '
                             'LOD coverage and native compatibility still require verification.' %
                             (len(_CACHE['candidates']), len(occupied)))
    return len(_CACHE['candidates'])

def _items(self, context):
    # Blender calls dynamic EnumProperty callbacks frequently. Return the
    # persistent immutable snapshot; never parse XML, FXOBJ or scene here.
    return _CACHE.get('enum_items') or [('NONE', 'No candidates (refresh first)', '')]

def _sync_ui(context, selected=None):
    """Rebuild the in-memory browser only on Refresh/Assign/Clear."""
    scene = context.scene
    previous = selected or getattr(scene, 'fh1_shader_browser_selected', '')
    entries = scene.fh1_shader_browser_entries
    entries.clear()
    occupied = _CACHE.get('occupied', set())
    for name in sorted(_CACHE['catalogue'], key=str.casefold):
        entry = entries.add()
        entry.name = name
        entry.shader_name = name
        entry.is_used = name.lower() in occupied
    names = [entry.shader_name for entry in entries]
    scene.fh1_shader_browser_index = names.index(previous) if previous in names else (0 if names else -1)
    if names:
        chosen = previous if previous in names else names[0]
        scene.fh1_selected_unused_shader = chosen
        scene.fh1_shader_browser_selected = chosen
    else:
        scene.fh1_shader_browser_selected = ''

def _recalculate_scene_availability(context, selected=None):
    occupied = {_name(m).lower() for m in _scene_materials() if _name(m)}
    _CACHE['occupied'] = occupied
    _CACHE['candidates'] = {name: deps for name, deps in _CACHE['catalogue'].items() if name.lower() not in occupied}
    _sync_ui(context, selected)

class FH1_PG_shader_entry(bpy.types.PropertyGroup):
    shader_name: StringProperty(name='Shader')
    is_used: BoolProperty(name='Used')

class FH1_UL_shader_browser(bpy.types.UIList):
    bl_idname = 'FH1_UL_shader_browser'
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # Blender 3.6: icons indicate status, not a re-evaluation of availability.
        layout.label(text=item.shader_name, icon='CHECKMARK' if item.is_used else 'RADIOBUT_OFF')
        layout.label(text='USED' if item.is_used else 'FREE')

class FH1_OT_browse_shaders(bpy.types.Operator):
    bl_idname = 'fh1_carbin.browse_shaders'
    bl_label = 'Browse Shaders'
    bl_description = 'Browse all cached shaders; checkmark = used, hollow circle = free'
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=470)
    def draw(self, context):
        scene = context.scene
        self.layout.label(text='\u2713 USED  /  \u25cb FREE  (used shaders require confirmation)')
        self.layout.template_list('FH1_UL_shader_browser', '', scene, 'fh1_shader_browser_entries', scene, 'fh1_shader_browser_index', rows=12)
        self.layout.label(text='%d shaders (cached)' % len(scene.fh1_shader_browser_entries))
    def execute(self, context):
        scene = context.scene
        idx = scene.fh1_shader_browser_index
        if idx < 0 or idx >= len(scene.fh1_shader_browser_entries):
            self.report({'WARNING'}, 'Refresh Availability first')
            return {'CANCELLED'}
        name = scene.fh1_shader_browser_entries[idx].shader_name
        scene.fh1_selected_unused_shader = name
        scene.fh1_shader_browser_selected = name
        return {'FINISHED'}

class FH1_OT_search_shaders(bpy.types.Operator):
    bl_idname = 'fh1_carbin.search_shaders'
    bl_label = 'Search Shaders'
    bl_description = 'Search the cached shader list by name; no rescan'
    query: StringProperty(name='Name contains', description='Case-insensitive substring of a shader name')
    shader: StringProperty(name='Exact shader name', description='Select a name from matching results, or type the exact name')
    def invoke(self, context, event):
        if not _CACHE['catalogue']:
            self.report({'WARNING'}, 'Refresh Availability first')
            return {'CANCELLED'}
        self.query = ''
        self.shader = ''
        return context.window_manager.invoke_props_dialog(self, width=470)
    def draw(self, context):
        layout = self.layout
        layout.prop(self, 'query', text='Find')
        matches = [name for name in sorted(_CACHE['catalogue'], key=str.casefold)
                   if self.query.casefold() in name.casefold()]
        layout.label(text='%d match(es); enter an exact name below' % len(matches))
        for name in matches[:12]:
            layout.label(text=name + ('  [USED]' if name.lower() in _CACHE.get('occupied', set()) else '  [FREE]'),
                         icon='CHECKMARK' if name.lower() in _CACHE.get('occupied', set()) else 'RADIOBUT_OFF')
        if len(matches) > 12:
            layout.label(text='Showing first 12; refine search to narrow results')
        layout.prop(self, 'shader', text='Select')
        if len(matches) == 1:
            layout.label(text='Leave Select empty to choose the sole match')
    def execute(self, context):
        matches = [name for name in _CACHE['catalogue'] if self.query.casefold() in name.casefold()]
        name = self.shader.strip()
        if not name and len(matches) == 1:
            name = matches[0]
        canonical = next((n for n in matches if n.casefold() == name.casefold()), None)
        if canonical is None:
            self.report({'WARNING'}, 'Enter an exact matching shader name (or narrow to one match)')
            return {'CANCELLED'}
        scene = context.scene
        scene.fh1_selected_unused_shader = canonical
        scene.fh1_shader_browser_selected = canonical
        for i, entry in enumerate(scene.fh1_shader_browser_entries):
            if entry.shader_name == canonical:
                scene.fh1_shader_browser_index = i
                break
        return {'FINISHED'}

def _fallback(mat):
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()
    output = nodes.new('ShaderNodeOutputMaterial')
    output.location = (250, 0)
    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    mat.node_tree.links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    bsdf.inputs['Base Color'].default_value = shader_preset_config.color_value({'hex': '#3C3C3C'})
    bsdf.inputs['Metallic'].default_value = 0
    bsdf.inputs['Specular'].default_value = 0
    bsdf.inputs['Roughness'].default_value = 1
    return bsdf

class FH1_OT_refresh_shaders(bpy.types.Operator):
    bl_idname = 'fh1_carbin.refresh_shader_availability'
    bl_label = 'Refresh Availability'
    bl_description = 'Rescan shader assignments across all mesh materials in the scene'
    def execute(self, context):
        root = _config_root(context)
        addon = context.preferences.addons.get(__package__)
        folder = bpy.path.abspath(getattr(addon.preferences, 'game_shader_folder', '')) if addon else ''
        count = refresh(root, folder) if root else 0
        if not root:
            _CACHE['message'] = 'Set Shared Car Textures in add-on preferences, or enable configuration override.'
        _sync_ui(context)
        self.report({'INFO'} if count else {'WARNING'}, _CACHE['message'])
        return {'FINISHED'}

class FH1_OT_assign_unused_shader(bpy.types.Operator):
    bl_idname = 'fh1_carbin.assign_unused_shader'
    bl_label = 'Assign Shader'
    bl_description = 'Assign native export metadata without modifying Blender material nodes'
    def invoke(self, context, event):
        mat = context.object.active_material if context.object else None
        if mat is None or mat.get('fh1_native_shared_shader') or mat.get('fh1_source_material_index') is not None:
            self.report({'ERROR'}, 'Select a new custom material, not an imported native material')
            return {'CANCELLED'}
        name = context.scene.fh1_shader_browser_selected
        if name not in _CACHE['catalogue']:
            self.report({'ERROR'}, 'Shader not in catalogue. Refresh Availability.')
            return {'CANCELLED'}
        if name.lower() in _CACHE.get('occupied', set()) or any(
                _name(m).lower() == name.lower() and m != mat for m in _scene_materials()):
            return context.window_manager.invoke_props_dialog(self, width=460)
        return self.execute(context)

    def draw(self, context):
        name = context.scene.fh1_shader_browser_selected
        self.layout.label(text='Shader "%s" is already used in this vehicle.' % name, icon='QUESTION')
        self.layout.label(text='Assign it to this custom material anyway?')
        self.layout.label(text='OK = assign; Cancel = leave material unchanged.')

    def execute(self, context):
        mat = context.object.active_material if context.object else None
        if mat is None or mat.get('fh1_native_shared_shader') or mat.get('fh1_source_material_index') is not None:
            self.report({'ERROR'}, 'Select a new custom material, not an imported native material')
            return {'CANCELLED'}
        name = context.scene.fh1_shader_browser_selected
        if name not in _CACHE['catalogue']:
            self.report({'ERROR'}, 'Shader not in catalogue. Refresh Availability.')
            return {'CANCELLED'}
        deps = _CACHE['catalogue'][name]
        occupied = name.lower() in _CACHE.get('occupied', set()) or any(
            _name(m).lower() == name.lower() and m != mat for m in _scene_materials())
        mat['fh1_custom_native_shader'] = name
        mat['fh1_shader_name'] = name
        mat['fh1_custom_texture_dependencies'] = '; '.join('%s/%s' % pair for pair in sorted(deps))
        mat['fh1_texture_state'] = 'not_provided'
        mat['fh1_custom_shader_provisional'] = bool(_CACHE['unresolved'] or _CACHE['warnings'].get(name))
        # Metadata only: preserve material nodes and texture previews.
        _recalculate_scene_availability(context, name)
        self.report({'INFO'}, 'Assigned %s%s; native rendering still requires in-game validation' %
                    (name, ' (already used elsewhere)' if occupied else ''))
        return {'FINISHED'}

class FH1_OT_clear_custom_shader(bpy.types.Operator):
    bl_idname = 'fh1_carbin.clear_custom_shader'
    bl_label = 'Clear Custom Shader'
    def execute(self, context):
        mat = context.object.active_material if context.object else None
        if not mat or not mat.get('fh1_custom_native_shader'):
            return {'CANCELLED'}
        for key in ('fh1_custom_native_shader','fh1_shader_name','fh1_custom_texture_dependencies','fh1_texture_state','fh1_custom_shader_provisional'):
            if key in mat: del mat[key]
        _recalculate_scene_availability(context, str(mat.get('fh1_resolved_shader') or ''))
        self.report({'INFO'}, 'Custom shader metadata cleared; availability updated from cache')
        return {'FINISHED'}

class FH1_PT_shader_controls(bpy.types.Panel):
    bl_label = 'FH1 CARBIN - Shader Controls (v5.0.4)'
    bl_idname = 'FH1_PT_shader_controls'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'material'
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.prop(scene, 'fh1_shader_config_override', text='Override configuration directory')
        col = layout.column()
        col.enabled = scene.fh1_shader_config_override
        col.prop(scene, 'fh1_shader_catalog_root', text='Configuration directory')
        if not scene.fh1_shader_config_override:
            root = _config_root(context)
            layout.label(text='Shared: ' + (bpy.path.basename(root.rstrip('/\\')) if root else 'Not configured'), icon='FILE_FOLDER')
        layout.operator('fh1_carbin.refresh_shader_availability', icon='FILE_REFRESH')
        for line in (_CACHE['message'][i:i+100] for i in range(0, len(_CACHE['message']), 100)):
            layout.label(text=line)
        layout.label(text='FXOBJ: ' + _CACHE['fxobj_note'][:95])
        row = layout.row(align=True)
        row.operator('fh1_carbin.browse_shaders', icon='PRESET', text='Browse Shaders')
        row.operator('fh1_carbin.search_shaders', icon='VIEWZOOM', text='Search Shaders')
        layout.label(text='Selected: ' + (scene.fh1_shader_browser_selected or 'None'))
        if scene.fh1_shader_browser_selected.lower() in _CACHE.get('occupied', set()):
            layout.label(text='USED: already assigned in scene', icon='CHECKMARK')
        selected = scene.fh1_shader_browser_selected
        if selected in _CACHE['catalogue']:
            deps = sorted(_CACHE['catalogue'][selected])
            layout.label(text='Status: ' + ('PROVISIONAL - testing allowed' if _CACHE['unresolved'] else 'Dependencies identified (LOD unverified)'))
            if selected in _CACHE['warnings']:
                layout.label(text='Shared dependency: see console / dropdown tooltip', icon='INFO')
            layout.label(text='Native: ' + _native_status(selected)[:90])
            for path, filename in deps[:5]:
                layout.label(text=filename + ' [' + path + ']')
        layout.operator('fh1_carbin.assign_unused_shader')
        layout.operator('fh1_carbin.clear_custom_shader')
        layout.label(text='Research: export binding not yet validated', icon='INFO')

_CLASSES = (FH1_PG_shader_entry, FH1_UL_shader_browser, FH1_OT_browse_shaders, FH1_OT_search_shaders, FH1_OT_refresh_shaders, FH1_OT_assign_unused_shader, FH1_OT_clear_custom_shader, FH1_PT_shader_controls)
def register():
    for cls in _CLASSES: bpy.utils.register_class(cls)
    bpy.types.Scene.fh1_shader_catalog_root = StringProperty(name='Shader configuration override', subtype='DIR_PATH')
    bpy.types.Scene.fh1_shader_config_override = BoolProperty(name='Override shader configuration', default=False)
    bpy.types.Scene.fh1_selected_unused_shader = StringProperty(name='Selected shader')
    bpy.types.Scene.fh1_shader_browser_selected = StringProperty(name='Browser selection')
    bpy.types.Scene.fh1_shader_browser_entries = CollectionProperty(type=FH1_PG_shader_entry)
    bpy.types.Scene.fh1_shader_browser_index = IntProperty(default=-1)
def unregister():
    del bpy.types.Scene.fh1_shader_browser_index
    del bpy.types.Scene.fh1_shader_browser_entries
    del bpy.types.Scene.fh1_shader_browser_selected
    del bpy.types.Scene.fh1_selected_unused_shader
    del bpy.types.Scene.fh1_shader_config_override
    del bpy.types.Scene.fh1_shader_catalog_root
    for cls in reversed(_CLASSES): bpy.utils.unregister_class(cls)
