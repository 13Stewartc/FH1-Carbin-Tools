"""Dev72 wheel previews for standalone wheels and wheel components in car LOD1-5."""
import os
import bpy
from bpy.props import EnumProperty
from . import xds_textures

WHEEL_MATS = ('rim', 'inner_rim', 'outer_rim', 'wheel_black')
BLUR_MATS = ('blur_lip', 'blur_rim')
TIRE_MATS = ('tire',)
FRAMES = tuple('wheel%d' % i for i in range(4))

def update_blur_controls(scene, context):
    """One scene-wide control drives both blur sections and every wheel instance."""
    for mat in bpy.data.materials:
        if not mat.get('fh1_wheel_blur_preview') or not mat.use_nodes:
            continue
        tree = mat.node_tree
        bsdf = tree.nodes.get('FH1 | Wheel Principled')
        if bsdf is None:
            continue
        for socket in ('Base Color', 'Alpha'):
            for link in tuple(bsdf.inputs[socket].links):
                tree.links.remove(link)
        frame = scene.fh1_wheel_blur_frame
        node = tree.nodes.get('FH1 | Blur ' + frame)
        if scene.fh1_wheel_blur_disabled:
            bsdf.inputs['Alpha'].default_value = 0.0
            mat['fh1_preview_source'] = 'blur disabled: transparent'
        elif node is not None and node.image is not None:
            tree.links.new(node.outputs['Color'], bsdf.inputs['Base Color'])
            tree.links.new(node.outputs['Alpha'], bsdf.inputs['Alpha'])
            bsdf.inputs['Alpha'].default_value = 1.0
            mat['fh1_preview_source'] = node.image.name
        else:
            bsdf.inputs['Alpha'].default_value = 0.0
            mat['fh1_preview_source'] = 'missing blur frame: transparent'


def register_properties():
    if not hasattr(bpy.types.Scene, 'fh1_wheel_blur_frame'):
        bpy.types.Scene.fh1_wheel_blur_frame = EnumProperty(
            name='Wheel Blur Layer',
            items=[(f, f+'.xds', 'Wheel blur frame '+f) for f in FRAMES],
            default='wheel0', update=update_blur_controls)
    if not hasattr(bpy.types.Scene, 'fh1_wheel_blur_disabled'):
        from bpy.props import BoolProperty
        bpy.types.Scene.fh1_wheel_blur_disabled = BoolProperty(
            name='Disable', description='Hide all wheel blur sections',
            default=True, update=update_blur_controls)


def unregister_properties():
    for prop in ('fh1_wheel_blur_disabled', 'fh1_wheel_blur_frame'):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)

def _wheel_file(carbin_path):
    # Source-specific detection; do not touch body/cockpit shaders named rim.
    filename = os.path.basename(carbin_path).lower()
    return any(bool(col.get('fh1_wheel_variant')) and
               str(col.get('fh1_source_file', '')).lower() == filename
               for col in bpy.data.collections)

def _image(filename, folder, shared_root, cache):
    for root in (folder, shared_root):
        if not root or not os.path.isdir(root):
            continue
        for walk, dirs, files in os.walk(root):
            dirs.sort()
            found = next((os.path.join(walk, f) for f in sorted(files)
                          if f.lower() == filename.lower()), None)
            if not found:
                continue
            try:
                if found not in cache:
                    cache[found] = xds_textures.load_xds_image(found)[0]
                return cache[found]
            except Exception as exc:
                print('[FH1 Wheel] Failed to decode %s: %s' % (found, exc))
    return None

def _ensure_preview(mat):
    mat.use_nodes = True
    tree = mat.node_tree
    nodes, links = tree.nodes, tree.links
    bsdf = nodes.get('FH1 | Wheel Principled')
    if bsdf is None:
        bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is None:
            bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.name = 'FH1 | Wheel Principled'
    bsdf.location = (0, 180)
    output = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
    if output is None:
        output = nodes.new('ShaderNodeOutputMaterial')
    output.location = (340, 180)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    for socket in ('Base Color','Alpha','Metallic','Specular','Roughness'):
        for link in tuple(bsdf.inputs[socket].links):
            links.remove(link)
    bsdf.inputs['Base Color'].default_value = (0.55, 0.57, 0.60, 1.0)
    bsdf.inputs['Metallic'].default_value = 1.0
    bsdf.inputs['Specular'].default_value = 0.5
    bsdf.inputs['Roughness'].default_value = 0.320
    bsdf.inputs['Alpha'].default_value = 1.0
    mat['fh1_template_applied'] = True
    return bsdf

def _image_node(mat, name, img, y):
    nodes = mat.node_tree.nodes
    node = nodes.get(name) or nodes.new('ShaderNodeTexImage')
    node.name = name
    node.label = name
    node.location = (-500, y)
    node.image = img
    return node

def _wheel_objects_for_file(carbin_path):
    """Find wheel components belonging to this imported CARBIN, not other open cars.

    Source-file identity is stored on the import root collection, so walk
    collection parents instead of relying on globally named Blender objects.
    """
    filename = os.path.basename(carbin_path).lower()
    parents = {}
    for parent in bpy.data.collections:
        for child in parent.children:
            parents.setdefault(child.as_pointer(), []).append(parent)
    matches = []
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or obj.get('fh1_source_lod') is None:
            continue
        lod = int(obj.get('fh1_source_lod'))
        source_name = str(obj.get('fh1_source_name', '')).lower()
        # Imported wheels use 'wheel'; car components can also be named
        # wheel_L1 ... wheel_L5 in the CARBIN hierarchy.
        if not (source_name == 'wheel' or
                (source_name.startswith('wheel_l') and source_name[-1:] in '12345')):
            continue
        pending = list(obj.users_collection)
        seen = set()
        while pending:
            col = pending.pop()
            ptr = col.as_pointer()
            if ptr in seen:
                continue
            seen.add(ptr)
            if str(col.get('fh1_source_file', '')).lower() == filename:
                matches.append((obj, lod))
                break
            pending.extend(parents.get(ptr, ()))
    return matches


def apply_wheel_materials(carbin_path, shared_root, cache):
    wheel_objects = _wheel_objects_for_file(carbin_path)
    if not wheel_objects:
        return
    folder = os.path.dirname(os.path.abspath(carbin_path))
    # One CARBIN carries wheel LOD0 and LOD1-5. Split only wheel material
    # datablocks, so LOD0 can use wheel_LOD0.xds and other LODs wheel.xds.
    standalone = _wheel_file(carbin_path)
    for obj, lod in wheel_objects:
        if not standalone and not 1 <= lod <= 5:
            continue
        for slot in obj.material_slots:
            source = slot.material
            if source is None:
                continue
            key = str(source.get('fh1_shader_name') or source.get('fh1_resolved_shader') or source.name).lower()
            if key.startswith('fh1_'): key = key[4:]
            if key.endswith('_lod0'): key = key[:-5]
            if key.endswith('_car_wheel'): key = key[:-10]
            if key not in WHEEL_MATS + BLUR_MATS + TIRE_MATS:
                continue
            # Keep car-wheel previews separate from standalone wheel datablocks.
            target_name = ('FH1_' + key + ('_LOD0' if lod == 0 else '')
                           + ('' if standalone else '_CAR_WHEEL'))
            mat = bpy.data.materials.get(target_name)
            if mat is None:
                mat = source.copy()
                mat.name = target_name
            mat['fh1_shader_name'] = key
            mat['fh1_resolved_shader'] = key
            slot.material = mat
            bsdf = _ensure_preview(mat)
            if key in WHEEL_MATS:
                filename = 'wheel_LOD0.xds' if lod == 0 else 'wheel.xds'
                img = _image(filename, folder, shared_root, cache)
                node = _image_node(mat, 'FH1 | Wheel atlas sample', img, 160)
                if img:
                    mat.node_tree.links.new(node.outputs['Color'], bsdf.inputs['Base Color'])
                    if lod == 0:
                        # wheel_LOD0.xds alpha carries specular strength; wheel.xds does not.
                        mat.node_tree.links.new(node.outputs['Alpha'], bsdf.inputs['Specular'])
                    mat['fh1_preview_source'] = img.name
                else:
                    mat['fh1_preview_source'] = 'no wheel atlas: metallic fallback'
            elif key in TIRE_MATS:
                # Tire-specific PBR settings, regardless of texture availability.
                bsdf.inputs['Metallic'].default_value = 0.0
                bsdf.inputs['Specular'].default_value = 0.0
                bsdf.inputs['Roughness'].default_value = 0.515
                # Tire preview uses a shared tireA0.xds through a default black-to-white
                # ColorRamp, with the white stop at 0.218. Preserve the sample and
                # ramp nodes even when the shared image is unavailable.
                img = _image('tireA0.xds', None, shared_root, cache)
                node = _image_node(mat, 'FH1 | Tire tireA0 sample', img, 160)
                nodes = mat.node_tree.nodes
                ramp = nodes.get('FH1 | Tire Color Ramp')
                if ramp is None or ramp.type != 'VALTORGB':
                    ramp = nodes.new('ShaderNodeValToRGB')
                ramp.name = 'FH1 | Tire Color Ramp'
                ramp.label = 'TireA0 contrast (white at 0.218)'
                ramp.location = (-250, 160)
                ramp.color_ramp.elements[0].position = 0.0
                ramp.color_ramp.elements[0].color = (0.0, 0.0, 0.0, 1.0)
                ramp.color_ramp.elements[1].position = 0.218
                ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
                ramp.color_ramp.interpolation = 'LINEAR'
                if img is not None:
                    mat.node_tree.links.new(node.outputs['Color'], ramp.inputs['Fac'])
                    mat.node_tree.links.new(ramp.outputs['Color'], bsdf.inputs['Base Color'])
                    mat['fh1_preview_source'] = img.name
                else:
                    for link in tuple(ramp.inputs['Fac'].links):
                        mat.node_tree.links.remove(link)
                    mat['fh1_preview_source'] = 'no shared tireA0.xds: tire PBR fallback'
            else:
                mat.blend_method = 'BLEND'
                mat.show_transparent_back = False
                mat['fh1_wheel_blur_preview'] = True
                for i, frame in enumerate(FRAMES):
                    img = _image(frame+'.xds', folder, shared_root, cache)
                    _image_node(mat, 'FH1 | Blur '+frame, img, 380 - 300*i)
                update_blur_controls(bpy.context.scene, None) if bpy.context.scene else None
            print('[FH1 Wheel] %s LOD%d: %s' % (key, lod, mat.get('fh1_preview_source')))
