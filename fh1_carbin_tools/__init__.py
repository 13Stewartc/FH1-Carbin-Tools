"""FH1 CARBIN Tools — Blender 3.6 import interface.

Import engines are preserved from tested standalone versions v0.8.4 and v0.15.3.
"""
bl_info = {
    "name": "FH1 CARBIN Tools v5.0.4 Used Shader Assignment Confirmation",
    "author": "FH1 CARBIN Tools",
    "version": (5, 0, 4),
    "blender": (3, 6, 0),
    "location": "File > Import > Forza Horizon 1 CARBIN (.carbin)",
    "description": "Import FH1 CARBIN body, wheel LOD0–5, cockpit and version-1 brake parts",
    "category": "Import-Export",
}

import os
import re
import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper
from . import shader_controls, material_templates, shape_key_audit, damage_donor, debug_secondary, import_lod, import_lod0, xds_textures, material_resolver, wheel_materials, export_carbin, export_positions, topology_audit, topology_plan, export_topology, export_model_pipeline, export_lod0_native, custom_shader_export

# Blender retains imported submodules across addon disable/re-enable and ZIP
# upgrades. Reload only the experimental texture/material modules so that
# an older xds_textures module cannot shadow the installed decoder.
import importlib
# Reload the audit module: Blender can otherwise retain Dev82 without inspect_selection.
shader_controls = importlib.reload(shader_controls)
xds_textures = importlib.reload(xds_textures)
material_templates = importlib.reload(material_templates)
material_resolver = importlib.reload(material_resolver)
wheel_materials = importlib.reload(wheel_materials)
# Always reload exporter modules on addon upgrade (Blender caches Python imports).
export_carbin = importlib.reload(export_carbin)
export_positions = importlib.reload(export_positions)
import_lod = importlib.reload(import_lod)
import_lod0 = importlib.reload(import_lod0)
topology_audit = importlib.reload(topology_audit)
topology_plan = importlib.reload(topology_plan)
export_topology = importlib.reload(export_topology)
export_model_pipeline = importlib.reload(export_model_pipeline)
debug_secondary = importlib.reload(debug_secondary)
export_lod0_native = importlib.reload(export_lod0_native)  # Hotfix 2: prevent cached Dev14 native writer
custom_shader_export = importlib.reload(custom_shader_export)


def detect_mode(filepath):
    # Version 1 is the verified rotor/caliper variant; don't depend on filename.
    with open(filepath, 'rb') as stream:
        header = stream.read(4)
    if len(header) != 4:
        raise ValueError('File too short for CARBIN header')
    version = int.from_bytes(header, 'big')
    if version == 1:
        return 'LOD0'
    if version == 3:
        # Wheel CARBINs have a version-3 outer header but use the LOD mesh importer.
        return 'LOD'
    if version != 5:
        raise ValueError('Unsupported CARBIN header/version %d (0x%s)' % (version, header.hex()))
    stem = os.path.splitext(os.path.basename(filepath))[0]
    if re.search(r'(?i)_cockpit$', stem):
        return 'LOD0'
    if re.search(r'(?i)_lod0$', stem):
        return 'LOD0'
    # Ordinary carbin files contain LOD1–5 sections. A renamed LOD0 file
    # should be imported with the explicit LOD0 mode instead.
    return 'LOD'


def collapse_import_hierarchy():
    """Fold nested collections after the file browser has closed.

    ImportHelper.execute() runs in the file-browser screen, which usually has
    no Outliner. Defer this UI-only operation until Blender restores the main
    workspace. Blender exposes no direct per-collection expand-state API.
    """
    def fold_outliners():
        found = False
        for window in bpy.context.window_manager.windows:
            screen = window.screen
            for area in screen.areas:
                if area.type != 'OUTLINER':
                    continue
                region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                if region is None:
                    continue
                found = True
                try:
                    with bpy.context.temp_override(window=window, area=area, region=region):
                        # Collapse nested objects/components, then expose roots.
                        # Repeated close calls reach the top regardless of depth.
                        for _ in range(24):
                            bpy.ops.outliner.show_one_level(open=False)
                        # Scene Collection -> vehicle root; leave LOD children folded.
                        bpy.ops.outliner.show_one_level(open=True)
                        bpy.ops.outliner.show_one_level(open=True)
                except (RuntimeError, AttributeError, TypeError) as exc:
                    print('[FH1 CARBIN Tools Dev57 v0.4.56] Outliner fold failed:', exc)
        if not found:
            print('[FH1 CARBIN Tools Dev57 v0.4.56] No Outliner available to fold')
        return None

    # Next main-loop tick, after the ImportHelper file browser closes.
    bpy.app.timers.register(fold_outliners, first_interval=0.2)


class FH1CARBIN_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__ or 'fh1_carbin_tools'

    damage_copy_nearest: BoolProperty(name='DEV18: Copy Nearest Damage Records', default=False, description='OFF = exact Dev17 zero padding for LOD1-5 and LOD0/cockpit. ON = experimental nearest-donor records; use disposable exports')

    damage_donor_mode: EnumProperty(name='DEV18 Damage Donor', items=[('SAME', 'Same component (nearest)', 'Copy records from the mesh being exported'), ('REAR', 'Rear bumper (experimental)', 'Copy records from nearest original rear bumper vertices')], default='SAME')

    shape_record_experiment: BoolProperty(name='DEV25: Shape Key Damage Bytes (EXPERIMENTAL)', default=False, description='Unverified XYZ signed-byte damage records; may crash the game. Use disposable exports only')
    shape_record_range: EnumProperty(name='Shape Delta Range', items=[('002', '2 cm', 'Signed bytes represent -2 to +2 cm'), ('010', '10 cm', 'Signed bytes represent -10 to +10 cm'), ('050', '50 cm', 'Signed bytes represent -50 to +50 cm')], default='002')

    debug_mode: BoolProperty(name='Debug Mode', default=False, description='Show legacy and diagnostic export tools in File > Export')

    beta_include_textures: BoolProperty(
        name='BETA: Include Textures',
        description='Bulk-load all vehicle XDS files only when sampler resolution is OFF; sampler resolution loads only referenced images',
        default=False,
    )

    beta_assign_materials: BoolProperty(name='BETA: Resolve Material Samplers', default=False,
        description='Experimental: use shared XML definitions to create image texture nodes')
    game_shader_folder: StringProperty(name='Game Shader Folder', subtype='DIR_PATH',
        description='Folder containing extracted native FXOBJ libraries (no original ZIP required)')

    shared_texture_folder: StringProperty(name='Shared Car Textures', subtype='DIR_PATH',
        description='Extracted shared folder containing textures.xml and ShaderSettings/Normal.xml')

    def draw(self, context):
        layout = self.layout
        layout.label(text='Installed build: v5.0.4 Used Shader Assignment Confirmation | 5.0.3', icon='INFO')
        beta = layout.box()
        beta.label(text='Beta Features', icon='EXPERIMENTAL')
        beta.prop(self, 'beta_include_textures')
        beta.prop(self, 'beta_assign_materials')
        beta.prop(self, 'shared_texture_folder')
        beta.prop(self, 'game_shader_folder')
        beta.label(text='Sampler resolution loads only referenced XDS files; bulk loading is skipped.')

        layout.prop(self, 'debug_mode')
        if self.debug_mode:
            debug = layout.box()
            debug.label(text='Debug', icon='TOOL_SETTINGS')
            debug.prop(self, 'shape_record_experiment')
            if self.shape_record_experiment:
                debug.prop(self, 'shape_record_range')
                debug.label(text='UNVERIFIED FORMAT - test copies only', icon='ERROR')
            debug.prop(self, 'damage_copy_nearest')
            if self.damage_copy_nearest:
                debug.prop(self, 'damage_donor_mode')


def textures_enabled(context):
    addon = context.preferences.addons.get(__package__ or 'fh1_carbin_tools')
    return bool(addon and addon.preferences.beta_include_textures)


class IMPORT_SCENE_OT_fh1_carbin(bpy.types.Operator, ImportHelper):
    """Import Forza Horizon 1 CARBIN meshes"""
    bl_idname = 'import_scene.fh1_carbin'
    bl_label = 'Import FH1 CARBIN'
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = '.carbin'
    filter_glob: StringProperty(default='*.carbin', options={'HIDDEN'})

    import_mode: EnumProperty(
        name='File type',
        description='Choose CARBIN format (Auto uses filename suffix and validates header)' ,
        items=[
            ('AUTO', 'Auto (filename)', 'Use _lod0/_cockpit suffix; otherwise LOD1–5'),
            ('LOD', 'LOD1–LOD5', 'Standard LOD file with explicit section LOD IDs 1–5'),
            ('LOD0', 'LOD0 / Cockpit / Parts', 'LOD0, cockpit, or version-1 brake part with section LOD ID 0'),
        ],
        default='AUTO',
    )
    lod1: BoolProperty(name='LOD1', default=True)
    lod2: BoolProperty(name='LOD2', default=True)
    lod3: BoolProperty(name='LOD3', default=True)
    lod4: BoolProperty(name='LOD4', default=True)
    lod5: BoolProperty(name='LOD5', default=True)
    report_topology: BoolProperty(
        name='Topology diagnostics',
        description='Print topology diagnostics to the system console (LOD1–5 only)',
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, 'import_mode')
        layout.label(text='LOD1–5 filters (ignored for LOD0 / Cockpit):')
        row = layout.row(align=True)
        for i in range(1, 6):
            row.prop(self, 'lod%d' % i, text=str(i), toggle=True)
        layout.prop(self, 'report_topology')
        layout.label(text='Texture beta option: Edit > Preferences > Add-ons > FH1 CARBIN Tools')

    def execute(self, context):
        filepath = bpy.path.abspath(self.filepath)
        if not os.path.isfile(filepath):
            self.report({'ERROR'}, 'File not found')
            return {'CANCELLED'}
        before_objects = set(bpy.data.objects)
        try:
            mode = detect_mode(filepath) if self.import_mode == 'AUTO' else self.import_mode
            print('[FH1 CARBIN Tools Dev57 v0.4.56] file=%s mode=%s' % (filepath, mode))
            if mode == 'LOD0':
                # The tested LOD0 parser determines cockpit vs exterior from
                # the source filename, preserving its original behaviour.
                count, failures = import_lod0.import_blender(filepath)
                if count == 0:
                    raise ValueError('No compatible mesh components found')
                if failures:
                    self.report({'WARNING'}, 'Imported %d components; %d skipped (see console)' % (count, len(failures)))
                else:
                    self.report({'INFO'}, 'Imported %d LOD0/cockpit components' % count)
            else:
                selected = tuple(i for i in range(1, 6) if getattr(self, 'lod%d' % i))
                if not selected:
                    self.report({'ERROR'}, 'Select at least one LOD')
                    return {'CANCELLED'}
                old_lods = import_lod.IMPORT_LODS
                old_report = import_lod.REPORT_TOPOLOGY
                try:
                    import_lod.IMPORT_LODS = selected
                    import_lod.REPORT_TOPOLOGY = self.report_topology
                    import_lod.import_file(filepath)
                finally:
                    import_lod.IMPORT_LODS = old_lods
                    import_lod.REPORT_TOPOLOGY = old_report
                self.report({'INFO'}, 'LOD import finished (check console for any skipped parts)')
        except Exception as exc:
            self.report({'ERROR'}, 'FH1 Tools v0.1.6 import failed: %s' % exc)
            return {'CANCELLED'}
        addon = context.preferences.addons.get(__package__ or 'fh1_carbin_tools')
        prefs = addon.preferences if addon else None
        # Sampler mode is demand-driven: never bulk-decode the entire car first.
        if textures_enabled(context) and not (prefs and prefs.beta_assign_materials):
            try:
                # Refresh the experimental decoder in case Blender cached a
                # previous version during an addon upgrade.
                decoder = importlib.reload(xds_textures)
                loaded, failures = decoder.load_xds_images(filepath)
                print('[FH1 XDS] Decoded %d XDS images; %d failures' % (len(loaded), len(failures)))
                self.report({'INFO'}, 'Decoded %d XDS images; %d failures' % (len(loaded), len(failures)))
            except Exception as exc:
                print('[FH1 XDS] Decoder error:', repr(exc))
                self.report({'WARNING'}, 'XDS loading failed (geometry preserved): %s' % exc)
        if prefs and prefs.beta_assign_materials:
            try:
                is_cockpit = bool(re.search(r'(?i)_cockpit\.carbin$', filepath))
                stats = material_resolver.apply_materials(filepath, bpy.path.abspath(prefs.shared_texture_folder),
                    cockpit=is_cockpit, superlod=False)
                print('[FH1 Materials] %s' % stats)
                self.report({'INFO'}, 'Material prototype: %d image nodes, %d diffuse links, %d errors' %
                    (stats['image_nodes'], stats['diffuse_links'], len(stats['errors'])))
            except Exception as exc:
                self.report({'WARNING'}, 'Material prototype failed (geometry imported): %s' % exc)
                print('[FH1 Materials] Failed:', exc)
        try:
            new_objects = [obj for obj in bpy.data.objects if obj not in before_objects and obj.type == 'MESH']
            export_carbin.snapshot_import(None, filepath, new_objects)
            self.report({'INFO'}, 'Export baseline saved for %d new meshes' % len(new_objects))
        except Exception as exc:
            self.report({'WARNING'}, 'Export baseline unavailable: %s' % exc)
        collapse_import_hierarchy()
        return {'FINISHED'}



class EXPORT_SCENE_OT_fh1_carbin_baseline(bpy.types.Operator, ExportHelper):
    """Byte-identical round-trip ONLY; refuses mesh edits."""
    bl_idname = 'export_scene.fh1_carbin_baseline'
    bl_label = 'FH1 CARBIN (Unmodified Baseline Only)'
    filename_ext = '.carbin'
    filter_glob: StringProperty(default='*.carbin', options={'HIDDEN'})

    def execute(self, context):
        try:
            if not context.active_object:
                raise ValueError('Select a tracked FH1 mesh')
            size, digest = export_carbin.lossless_export(context.active_object, bpy.path.abspath(self.filepath))
            self.report({'INFO'}, 'Byte-identical export: %d bytes, SHA256 %s' % (size, digest[:12]))
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class EXPORT_SCENE_OT_fh1_carbin_positions(bpy.types.Operator, ExportHelper):
    """Experimental: export vertex-position edits only; no new geometry"""
    bl_idname = 'export_scene.fh1_carbin_positions'
    bl_label = 'FH1 CARBIN (Vertex Positions — Experimental)'
    filename_ext = '.carbin'
    filter_glob: StringProperty(default='*.carbin', options={'HIDDEN'})

    def execute(self, context):
        try:
            if not context.active_object:
                raise ValueError('Select a tracked FH1 mesh')
            n, size, digest = export_positions.export_positions(context.active_object, bpy.path.abspath(self.filepath))
            self.report({'INFO'}, 'Position export: %d vertices, %d bytes, SHA256 %s' % (n, size, digest[:12]))
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class FH1CARBIN_OT_topology_audit(bpy.types.Operator):
    """Inspect Ctrl+J joined geometry and original CARBIN provenance. No file is written."""
    bl_idname = 'fh1_carbin.audit_joined_geometry'
    bl_label = 'FH1 CARBIN: Audit Joined Geometry'

    def execute(self, context):
        try:
            added_v, added_f, old_v, old_f = topology_audit.audit(context.active_object)
            self.report({'INFO'}, 'FH1 v0.2.8: %d new vertices, %d new faces; %d original vertices, %d original faces' %
                        (added_v, added_f, old_v, old_f))
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, '[FH1 v0.2.8] ' + str(exc))
            return {'CANCELLED'}


class FH1CARBIN_OT_topology_plan(bpy.types.Operator, ExportHelper):
    bl_idname = 'fh1_carbin.topology_plan'
    bl_label = 'FH1 CARBIN: Save Topology Rebuild Plan (Experimental)'
    bl_description = 'Save validated Ctrl+J vertex and triangle mapping; does not export a CARBIN yet'
    filename_ext = '.json'
    filter_glob: StringProperty(default='*.json', options={'HIDDEN'})

    def execute(self, context):
        try:
            result = topology_plan.write_plan(context.active_object, self.filepath)
            self.report({'INFO'}, 'Planned %d new vertices, %d new faces (JSON only)' %
                        (result['new_vertex_count'], result['new_faces']))
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class FH1CARBIN_OT_export_joined(bpy.types.Operator, ExportHelper):
    bl_idname = 'fh1_carbin.export_joined'
    bl_label = 'FH1 CARBIN: Export Joined Geometry (VERY EXPERIMENTAL)'
    bl_description = 'Experimental new-vertex writer for LOD1-5; export a COPY and validate in Forza Studio'
    filename_ext = '.carbin'
    filter_glob: StringProperty(default='*.carbin', options={'HIDDEN'})

    def execute(self, context):
        if not self.filepath.lower().endswith('.carbin'):
            self.filepath += '.carbin'
        try:
            experiment = custom_shader_export.Experiment(context.active_object, context.active_object.get('fh1_export_source_path',''), False)
            try:
                v, f, err, size, moved = export_topology.export_joined(context.active_object, self.filepath)
                experiment.finalize(self.filepath)
                size = os.path.getsize(self.filepath)
            except Exception:
                if experiment.targets and os.path.exists(self.filepath):
                    os.unlink(self.filepath)
                raise
            finally:
                experiment.close()
            self.report({'INFO'}, 'Wrote %d new vertices, %d triangles, %d moved originals; error %.6f (%d bytes)' % (v, f, moved, err, size))
            return {'FINISHED'}
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, '[v0.3.1] ' + str(exc))
            return {'CANCELLED'}


def _fh1_shape_key_items(self, context):
    obj = context.active_object if context else None
    items = [('NONE', 'None (donor records only)', 'Do not inspect a custom damage shape')]
    if obj and obj.type == 'MESH' and obj.data.shape_keys:
        basis = obj.data.shape_keys.reference_key
        for key in obj.data.shape_keys.key_blocks:
            if key != basis:
                items.append((key.name, key.name, 'Inspect this authored damage shape (research only)'))
    return items


def _fh1_shape_key_audit(obj, name):
    if name == 'NONE':
        return
    from . import shape_key_audit
    result = shape_key_audit.audit(obj, name)
    print('[FH1 Dev22 SHAPE KEY AUDIT] %s' % result)


def _fh1_deformation_source_items(self, context):
    """List source entries from the current imported vehicle, not a hardcoded list."""
    items = [('OFF', 'None (zero records)', 'Append neutral records to new vertices'),
             ('SAME', 'Same part (nearest)', 'Use original vertices of each part being exported')]
    obj = context.active_object if context else None
    if obj is None:
        return items
    source = str(obj.get('fh1_export_source_path', ''))
    digest = str(obj.get('fh1_export_source_sha256', ''))
    if not source:
        return items
    entries = {}
    for candidate in bpy.data.objects:
        if candidate.type != 'MESH' or str(candidate.get('fh1_export_source_path', '')) != source or str(candidate.get('fh1_export_source_sha256', '')) != digest:
            continue
        name = candidate.name.rsplit('_L', 1)[0] if re.search(r'_L[0-5]$', candidate.name) else candidate.name
        if candidate.get('fh1_native_vertex_data_offset') is not None:
            identity = 'N:%d' % (int(candidate['fh1_native_vertex_data_offset']) - 20)
        else:
            identity = 'P:' + name
        entries[identity] = name
    for key, name in sorted(entries.items(), key=lambda pair: pair[1].casefold()):
        items.append((key, name, 'Use original secondary records from ' + name))
    return items


class FH1CARBIN_OT_export_model(bpy.types.Operator, ExportHelper):
    bl_idname = 'fh1_carbin.export_model'
    bl_label = 'FH1 CARBIN: Export Model'
    bl_description = 'Export tracked FH1 model edits; unsupported LOD0/cockpit geometry changes are blocked'
    filename_ext = '.carbin'
    filter_glob: StringProperty(default='*.carbin', options={'HIDDEN'})
    deformation_source: EnumProperty(name='Deformation Source', items=_fh1_deformation_source_items, description='Choose the original part supplying deformation records for newly added vertices', default=None)
    deformation_shape: EnumProperty(name='Damage Shape Key (Research)', items=_fh1_shape_key_items, description='Inspect authored deformation; no custom damage encoding yet')

    def invoke(self, context, event):
        obj = context.active_object
        if obj and obj.type == 'MESH':
            name = obj.fh1_damage_shape_ui
            if name in [item[0] for item in _fh1_shape_key_items(self, context)]:
                self.deformation_shape = name
        return ExportHelper.invoke(self, context, event)

    def draw(self, context):
        self.layout.prop(self, 'deformation_source')
        if _debug_enabled(context):
            debug = self.layout.box()
            debug.label(text='Debug', icon='TOOL_SETTINGS')
            debug.prop(self, 'deformation_shape')
            debug.label(text='Shape key bytes are unverified; donor records remain unchanged', icon='INFO')

    def execute(self, context):
        if not self.filepath.lower().endswith('.carbin'):
            self.filepath += '.carbin'
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, 'Select a tracked FH1 mesh')
            return {'CANCELLED'}
        if _debug_enabled(context):
            try:
                _fh1_shape_key_audit(obj, self.deformation_shape)
            except Exception as exc:
                self.report({'ERROR'}, 'Shape key audit: ' + str(exc))
                return {'CANCELLED'}
        source = str(obj.get('fh1_export_source_path', ''))
        # The established LOD1-5 writer validates source provenance itself.
        # Do not pass LOD0/cockpit through its 16-bit section writer.

        try:
            lod0 = obj.get('fh1_native_vertex_format') == 'post-section-28-byte'
            experiment = custom_shader_export.Experiment(obj, source, lod0)
            try:
                with damage_donor.export_selection(self.deformation_source):
                    if lod0:
                        v, f, err, size, moved = export_lod0_native.export_model(obj, self.filepath)
                    else:
                        v, f, err, size, moved = export_model_pipeline.export_model(obj, self.filepath)
                experiment.finalize(self.filepath)
                size = os.path.getsize(self.filepath)
            except Exception:
                if experiment.targets and os.path.exists(self.filepath):
                    os.unlink(self.filepath)
                raise
            finally:
                experiment.close()
            self.report({'INFO'}, 'Dev24 Visible Damage UI: Exported %d new vertices, %d new triangles, %d edited source vertices (%d bytes)' % (v, f, moved, size))
            return {'FINISHED'}
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def _debug_enabled(context):
    addon = context.preferences.addons.get(__package__ or 'fh1_carbin_tools')
    return bool(addon and addon.preferences.debug_mode)


def menu_func_audit(self, context):
    if not _debug_enabled(context):
        return
    self.layout.operator(FH1CARBIN_OT_topology_audit.bl_idname, text='FH1 CARBIN: Audit Joined Geometry')
    self.layout.operator(FH1CARBIN_OT_topology_plan.bl_idname, text='FH1 CARBIN: Save Topology Rebuild Plan')
    self.layout.operator(FH1CARBIN_OT_export_joined.bl_idname, text='FH1 CARBIN: Export Joined Geometry (Experimental)')


def menu_func_export(self, context):
    self.layout.operator(FH1CARBIN_OT_export_model.bl_idname, text='FH1 CARBIN: Export Model')
    if _debug_enabled(context):
        self.layout.operator(EXPORT_SCENE_OT_fh1_carbin_positions.bl_idname, text='FH1 CARBIN (Vertex Positions — Experimental)')
        self.layout.operator(EXPORT_SCENE_OT_fh1_carbin_baseline.bl_idname, text='FH1 CARBIN (Unmodified Baseline Only)')


def menu_func_import(self, context):
    if _debug_enabled(context):
        self.layout.operator(debug_secondary.FH1_OT_secondary_as_mesh.bl_idname, text='Secondary Vertex Stream (Debug) (.carbin)')
    self.layout.operator(IMPORT_SCENE_OT_fh1_carbin.bl_idname,
                         text='Forza Horizon 1 CARBIN (.carbin)')


# User-facing material override. Store the choice in the existing ID property so
# it survives save/reload and is honoured by the resolver on re-import.
_ALPHA_ITEMS = (
    ('AUTO', 'Automatic', 'Use the shader-family preview policy'),
    ('OPAQUE', 'Opaque', 'Ignore image alpha'),
    ('CUTOUT', 'Alpha Hashed (Cutout)', 'Use the texture alpha as a hashed cutout'),
    ('BLEND', 'Alpha Blend', 'Use texture alpha with alpha blending'),
)


_SPECULAR_ITEMS = (
    ('PRESET', 'Shader Preset', 'Use the existing FH1 material specular values and texture routing'),
    ('TEXTURE_ALPHA', 'Texture Alpha → Specular', 'Use base-colour texture alpha to drive Principled BSDF Specular'),
    ('ALPHA_INVERT_MIX', 'Diffuse Alpha Detail Blend', 'Blend diffuse alpha with its inverse at 50% to reproduce the preferred detail preview'),
)


def _get_specular_choice(self):
    key = str(self.get('fh1_specular_override', 'PRESET')).upper()
    if str(self.get('fh1_alpha_override', '')).upper() == 'TEXTURE_ALPHA_SPECULAR':
        return 1  # Legacy v0.1.12 setting
    return next((i for i, item in enumerate(_SPECULAR_ITEMS) if item[0] == key), 0)


def _set_specular_choice(self, value):
    self['fh1_specular_override'] = _SPECULAR_ITEMS[max(0, min(int(value), len(_SPECULAR_ITEMS)-1))][0]
    _refresh_material_rendering(self)


def _get_alpha_choice(self):
    key = str(self.get('fh1_alpha_override', 'AUTO')).upper()
    if key == 'TEXTURE_ALPHA_SPECULAR':
        key = 'OPAQUE'
    return next((i for i, item in enumerate(_ALPHA_ITEMS) if item[0] == key), 0)


def _refresh_material_rendering(self):
    if not self.use_nodes or not self.node_tree:
        return
    bsdf = next((n for n in self.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is None:
        return
    images = [n for n in self.node_tree.nodes if n.type == 'TEX_IMAGE' and n.image]
    source = str(self.get('fh1_resolved_samplers', '')).split(';')[0].strip()
    image = next((n for n in images if n.name == 'FH1 | ' + source), None)
    if image is None:
        image = next((n for n in images if 'Interior' in n.name or 'Diffuse' in n.name), None)
    if image is None and images:
        image = images[0]
    shader = str(self.get('fh1_resolved_shader', 'solid'))
    material_resolver._alpha_mode(self, bsdf, image, source, shader)


def _set_alpha_choice(self, value):
    self['fh1_alpha_override'] = _ALPHA_ITEMS[max(0, min(int(value), len(_ALPHA_ITEMS)-1))][0]
    _refresh_material_rendering(self)


class MATERIAL_PT_fh1_alpha(bpy.types.Panel):
    bl_label = 'FH1 Material Rendering'
    bl_idname = 'MATERIAL_PT_fh1_alpha'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'material'

    @classmethod
    def poll(cls, context):
        mat = context.material
        return mat is not None and 'fh1_resolved_shader' in mat

    def draw(self, context):
        mat = context.material
        col = self.layout.column(align=True)
        col.prop(mat, 'fh1_alpha_mode_ui', text='Alpha Mode')
        col.prop(mat, 'fh1_specular_mode_ui', text='Specular Mode')
        col.label(text='Auto uses a preview policy, not verified GPU states.', icon='INFO')
        col.label(text='Current: ' + str(mat.get('fh1_alpha_mode', 'Not evaluated')))



class FH1_MT_debug_tools(bpy.types.Menu):
    bl_label = 'FH1 Debug'
    bl_idname = 'FH1_MT_debug_tools'

    def draw(self, context):
        self.layout.operator('fh1.debug_secondary_as_mesh', icon='MESH_DATA')


def fh1_debug_menu(self, context):
    self.layout.menu('FH1_MT_debug_tools', icon='TOOL_SETTINGS')



class FH1CARBIN_PT_shape_key_export(bpy.types.Panel):
    bl_label = 'FH1 Custom Damage Research (Dev24)'
    bl_idname = 'FH1CARBIN_PT_shape_key_export'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'FH1 CARBIN'

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(_debug_enabled(context) and obj and obj.type == 'MESH')

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        layout.label(text='Damage Research (Debug) | Dev35', icon='INFO')
        if not obj.data.shape_keys or len(obj.data.shape_keys.key_blocks) < 2:
            layout.label(text='No non-Basis shape keys on mesh', icon='ERROR')
        else:
            layout.prop_search(obj, 'fh1_damage_shape_ui', obj.data.shape_keys, 'key_blocks', text='Damage Shape Key')
            layout.operator('fh1.audit_selected_damage_shape', text='Audit Selected Shape', icon='VIEWZOOM')
        layout.operator('fh1_carbin.export_model', text='Export FH1 CARBIN', icon='EXPORT')
        layout.label(text='Dev25: enable Shape Key Damage Bytes in Add-on Preferences')


class FH1CARBIN_OT_audit_selected_damage_shape(bpy.types.Operator):
    bl_idname = 'fh1.audit_selected_damage_shape'
    bl_label = 'Audit Selected Damage Shape'
    def execute(self, context):
        obj = context.active_object
        name = obj.fh1_damage_shape_ui
        if not name or name == 'NONE':
            self.report({'WARNING'}, 'Select a non-Basis shape key first')
            return {'CANCELLED'}
        try:
            result = shape_key_audit.audit(obj, name)
            print('[FH1 Dev23 SHAPE KEY AUDIT] %s' % result)
            self.report({'INFO'}, 'Shape %s: %d changed vertices, max displacement %.6f (see console)' % (name, result['changed_vertices'], result['maximum_displacement']))
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def _fh1_object_shape_get(self):
    name = self.get('fh1_damage_shape_selection', 'NONE')
    choices = [i[0] for i in _fh1_shape_key_items(None, bpy.context)] if bpy.context.active_object == self else ['NONE']
    return choices.index(name) if name in choices else 0

def _fh1_object_shape_set(self, value):
    choices = [i[0] for i in _fh1_shape_key_items(None, bpy.context)]
    self['fh1_damage_shape_selection'] = choices[value] if 0 <= value < len(choices) else 'NONE'



class FH1CARBIN_PT_shape_key_data(bpy.types.Panel):
    bl_label = 'FH1 Damage Research — Dev24'
    bl_idname = 'FH1CARBIN_PT_shape_key_data'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'data'

    @classmethod
    def poll(cls, context):
        return bool(_debug_enabled(context) and context.active_object and context.active_object.type == 'MESH')

    def draw(self, context):
        FH1CARBIN_PT_shape_key_export.draw(self, context)

def register():
    wheel_materials.register_properties()
    shader_controls.register()
    print('[FH1 CARBIN Tools Dev87 / v0.4.88] Add-on registered')
    bpy.utils.register_class(debug_secondary.FH1_OT_secondary_as_mesh)
    bpy.utils.register_class(FH1_MT_debug_tools)
    bpy.utils.register_class(FH1CARBIN_AddonPreferences)
    bpy.utils.register_class(IMPORT_SCENE_OT_fh1_carbin)
    bpy.utils.register_class(EXPORT_SCENE_OT_fh1_carbin_baseline)
    bpy.utils.register_class(EXPORT_SCENE_OT_fh1_carbin_positions)
    bpy.utils.register_class(FH1CARBIN_OT_topology_audit)
    bpy.utils.register_class(FH1CARBIN_OT_topology_plan)
    bpy.utils.register_class(FH1CARBIN_OT_export_joined)
    bpy.utils.register_class(FH1CARBIN_OT_export_model)
    bpy.types.Object.fh1_damage_shape_ui = StringProperty(name='Damage Shape Key', description='Name of any non-Basis shape key; research only')
    bpy.utils.register_class(FH1CARBIN_OT_audit_selected_damage_shape)
    bpy.utils.register_class(FH1CARBIN_PT_shape_key_export)
    bpy.utils.register_class(FH1CARBIN_PT_shape_key_data)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_audit)
    bpy.types.Material.fh1_alpha_mode_ui = EnumProperty(name='Alpha Mode', items=_ALPHA_ITEMS, get=_get_alpha_choice, set=_set_alpha_choice)
    bpy.types.Material.fh1_specular_mode_ui = EnumProperty(name='Specular Mode', items=_SPECULAR_ITEMS, get=_get_specular_choice, set=_set_specular_choice)
    bpy.utils.register_class(MATERIAL_PT_fh1_alpha)
    bpy.utils.register_class(material_templates.FH1CARBIN_PT_template_controls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    shader_controls.unregister()
    wheel_materials.unregister_properties()
    bpy.utils.unregister_class(FH1_MT_debug_tools)
    bpy.utils.unregister_class(debug_secondary.FH1_OT_secondary_as_mesh)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.utils.unregister_class(material_templates.FH1CARBIN_PT_template_controls)
    bpy.utils.unregister_class(MATERIAL_PT_fh1_alpha)
    del bpy.types.Material.fh1_specular_mode_ui
    del bpy.types.Material.fh1_alpha_mode_ui
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_audit)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.utils.unregister_class(FH1CARBIN_PT_shape_key_data)
    bpy.utils.unregister_class(FH1CARBIN_PT_shape_key_export)
    bpy.utils.unregister_class(FH1CARBIN_OT_audit_selected_damage_shape)
    del bpy.types.Object.fh1_damage_shape_ui
    bpy.utils.unregister_class(FH1CARBIN_OT_export_model)
    bpy.utils.unregister_class(FH1CARBIN_OT_export_joined)
    bpy.utils.unregister_class(FH1CARBIN_OT_topology_plan)
    bpy.utils.unregister_class(FH1CARBIN_OT_topology_audit)
    bpy.utils.unregister_class(EXPORT_SCENE_OT_fh1_carbin_positions)
    bpy.utils.unregister_class(EXPORT_SCENE_OT_fh1_carbin_baseline)
    bpy.utils.unregister_class(IMPORT_SCENE_OT_fh1_carbin)
    bpy.utils.unregister_class(FH1CARBIN_AddonPreferences)


if __name__ == '__main__':
    register()
