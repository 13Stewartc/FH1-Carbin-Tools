"""Experimental secondary-stream debug mesh, integrated with FH1 CARBIN Tools.
No changes to import/export geometry or damage handling.
"""
import bpy, struct
from bpy.props import StringProperty, EnumProperty, FloatProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper

def inspect_stream(data, rec, next_start):
    signature=struct.pack('>4I',3,rec['count'],4,1)
    candidates=[]; cursor=rec['end']
    while True:
        p=data.find(signature,cursor,next_start)
        if p<0:break
        if p+16+rec['count']*4<=next_start:candidates.append(p)
        cursor=p+1
    if len(candidates)!=1:raise ValueError('Expected one 4-byte stream for %s; found %d'%(rec['name'],len(candidates)))
    p=candidates[0]+16
    return [data[p+i*4:p+i*4+4] for i in range(rec['count'])]

class FH1_OT_secondary_as_mesh(bpy.types.Operator, ImportHelper):
    bl_idname = 'fh1.debug_secondary_as_mesh'
    bl_label = 'Debug: Import Secondary Stream as Verts'
    bl_description = 'Creates a separate vertex-only diagnostic mesh using original positions and experimental byte offsets; never modifies the source object'
    filename_ext = '.carbin'
    filter_glob: StringProperty(default='*.carbin', options={'HIDDEN'})
    interpretation: EnumProperty(name='Interpretation', items=[
        ('RAW', 'Raw positions', 'Vertex cloud at original positions, with four raw byte attributes'),
        ('SIGNED', 'Signed XYZ bytes', 'Experimental XYZ offsets from first three signed bytes; fourth byte preserved'),
        ('UNSIGNED', 'Unsigned centred XYZ', 'Experimental XYZ offsets from first three unsigned bytes minus 128')], default='RAW')
    offset_scale: FloatProperty(name='Offset scale', description='Experimental displacement per signed byte unit in Blender coordinates', default=0.001, min=0.000001, max=1.0, precision=5)
    make_shape_key: BoolProperty(name='Candidate shape key', description='For signed/unsigned modes create Basis and experimental offset shape key on diagnostic mesh', default=True)

    def execute(self, context):
        obj = context.active_object
        # File > Import should work directly from a CARBIN, without requiring
        # a previously selected mesh. Import the source when necessary.
        if (obj is None or obj.type != 'MESH' or
                'fh1_source_record_order' not in obj or
                'fh1_source_vertex_indices' not in obj ):
            before = set(bpy.data.objects)
            outcome = bpy.ops.import_scene.fh1_carbin(filepath=self.filepath, import_mode='LOD')
            if 'FINISHED' not in outcome:
                self.report({'ERROR'}, 'Source CARBIN import failed')
                return {'CANCELLED'}
            candidates = [o for o in bpy.data.objects if o not in before and o.type == 'MESH'
                          and 'fh1_source_record_order' in o and 'fh1_source_vertex_indices' in o]
            if not candidates:
                self.report({'ERROR'}, 'No compatible LOD1-5 mesh components were imported')
                return {'CANCELLED'}
            obj = candidates[0]
        try:
            from fh1_carbin_tools import import_lod
            with open(self.filepath, 'rb') as f:
                data = f.read()
            records = import_lod.discover(data)
            source_order = int(obj['fh1_source_record_order'])
            rec = records[source_order] if 0 <= source_order < len(records) else None
            if rec is None:
                raise ValueError('Cannot find matching source record')
            if rec['name'] != obj.get('fh1_source_name') or ('fh1_source_record_offset' in obj and rec['p'] != int(obj['fh1_source_record_offset'])):
                raise ValueError('CARBIN component does not match selected object. Select the same original CARBIN used for import.')
            ri = records.index(rec)
            bound = records[ri+1]['p'] if ri+1 < len(records) else len(data)
            raw = inspect_stream(data, rec, bound)
            ids = list(obj['fh1_source_vertex_indices'])
            if len(ids) != len(obj.data.vertices):
                raise ValueError('Source vertex mapping does not match mesh vertex count; import an unedited source object')
            if any(int(v) < 0 or int(v) >= len(raw) for v in ids):
                raise ValueError('Source vertex index outside secondary stream')
            base = [tuple(v.co) for v in obj.data.vertices]
            values = [raw[int(v)] for v in ids]
            mesh = bpy.data.meshes.new(obj.name + '_secondary_debug')
            # Point cloud: no faces. A diagnostic mesh is not a decoded damage target.
            mesh.from_pydata(base, [], [])
            mesh.update()
            for channel in range(4):
                attr = mesh.attributes.new(name='fh1_secondary_byte_%d' % channel, type='INT', domain='POINT')
                for i, value in enumerate(values):
                    attr.data[i].value = value[channel]
            nonzero = mesh.attributes.new(name='fh1_secondary_nonzero', type='INT', domain='POINT')
            for i, value in enumerate(values):
                nonzero.data[i].value = int(any(value))
            result = bpy.data.objects.new(obj.name + '_Secondary_Debug', mesh)
            context.collection.objects.link(result)
            result.matrix_world = obj.matrix_world.copy()
            result['fh1_debug_source_object'] = obj.name
            result['fh1_debug_source_file'] = self.filepath
            result['fh1_debug_interpretation'] = self.interpretation
            result['fh1_debug_warning'] = 'EXPERIMENTAL: four bytes are NOT verified XYZ offsets or native damage morphs'
            result['fh1_debug_stream_count'] = len(raw)
            if self.interpretation != 'RAW':
                offsets = []
                for value in values:
                    if self.interpretation == 'SIGNED':
                        xyz = [v if v < 128 else v-256 for v in value[:3]]
                    else:
                        xyz = [v-128 for v in value[:3]]
                    offsets.append(tuple(c*self.offset_scale for c in xyz))
                if self.make_shape_key:
                    result.shape_key_add(name='Basis', from_mix=False)
                    key = result.shape_key_add(name='FH1_Candidate_Bytes_NotVerified', from_mix=False)
                    for i, (p, delta) in enumerate(zip(base, offsets)):
                        key.data[i].co = tuple(p[k] + delta[k] for k in range(3))
                    key.value = 1.0
                else:
                    for i, delta in enumerate(offsets):
                        mesh.vertices[i].co = tuple(base[i][k]+delta[k] for k in range(3))
                    mesh.update()
            # Show points as visible dots via vertex selection in edit mode, not a face mesh.
            for selected in context.selected_objects:
                selected.select_set(False)
            result.select_set(True)
            context.view_layer.objects.active = result
            self.report({'INFO'}, 'Created %d diagnostic verts (%d nonzero); NOT verified deformation' % (len(ids), sum(any(x) for x in values)))
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

