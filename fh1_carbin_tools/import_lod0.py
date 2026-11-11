"""FH1 LOD0 and cockpit hierarchy preview v0.15.3 (cleanup only). Preserve native bounds and tiled UV transforms.
Reads positions, UVs, normals and per-component position bounds from CARBIN.
Read-only. Keeps the existing LOD1-5 v0.8 script unchanged.
"""
import hashlib
import math
import re
import struct
from pathlib import Path

RESTART = 0x00FFFFFF
RESTART_16 = 0xFFFF


def vehicle_key(path):
    """Shared name for VW_Corrado_95.carbin and VW_Corrado_95_lod0.carbin."""
    import os, re
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    stem = re.sub(r'(?i)(?:_lod[0-5]|_cockpit)$', '', stem)
    return re.sub(r'(?i)_(?:rotor|caliper)(?:LF|RF|LR|RR)$', '', stem)

def shared_root(bpy, path):
    key = vehicle_key(path)
    # Prefer a root tagged by either importer, then a pre-existing exact-name root.
    for collection in bpy.context.scene.collection.children:
        if collection.get('fh1_vehicle_key', '').casefold() == key.casefold():
            return collection
    for collection in bpy.context.scene.collection.children:
        if collection.name.casefold() == key.casefold():
            collection['fh1_vehicle_key'] = key
            return collection
    root = bpy.data.collections.new(key)
    bpy.context.scene.collection.children.link(root)
    root['fh1_vehicle_key'] = key
    return root

def child_collection(bpy, root, name):
    for collection in root.children:
        if collection.name == name:
            return collection
    collection = bpy.data.collections.new(name)
    root.children.link(collection)
    return collection

def u32(b, p): return struct.unpack_from('>I', b, p)[0]

def faces_from_strip(indices, restart=RESTART):
    result, strip = [], []
    for index in indices:
        if index >= restart:
            strip.clear(); continue
        strip.append(index)
        if len(strip) >= 3:
            a, b, c = strip[-3:]
            if len({a,b,c}) == 3:
                result.append((a,b,c) if len(strip)%2 else (b,a,c))
    return result

def find_sections(b, index_width=4):
    # Include valid index sections even when the provisional atlas probe fails.
    found = []
    for match in re.finditer(rb"\x00\x00\x00[\x02-\x30][A-Za-z0-9_]{2,48}", b):
        p = match.start()+4
        n = u32(b,p-4)
        if not (2 <= n <= 48 and p+n+80 < len(b)): continue
        name = b[p:p+n]
        if not all(32 <= c <= 126 for c in name): continue
        h = p+n
        if (u32(b,h+64),u32(b,h+68),u32(b,h+76)) != (4,0,index_width): continue
        count = u32(b,h+72)
        end = h+80+count*index_width
        if not (3 <= count <= 250000 and end <= len(b)): continue
        indices = struct.unpack_from(('>%dH' if index_width == 2 else '>%dI') % count,b,h+80)
        restart = RESTART_16 if index_width == 2 else RESTART
        valid = [i for i in indices if i < restart]
        if not valid or max(valid) > 1000000: continue
        atlas = struct.unpack_from('>4f',b,p-72) if p>=72 else (0,1,0,1)
        atlas_valid = all(math.isfinite(x) and abs(x)<1000000 for x in atlas)
        if not atlas_valid:
            # Geometry must not disappear just because its UV metadata differs.
            # Only non-finite or implausibly enormous atlas data uses a provisional fallback.
            atlas = (0.0, 1.0, 0.0, 1.0)
        # The next four floats are the independent UV1/second-channel atlas.
        # Its offsets and scales may be negative or far beyond 0..1 for tiled textures.
        atlas1 = struct.unpack_from('>4f', b, p-56) if p >= 56 else (0.0, 1.0, 0.0, 1.0)
        atlas1_valid = all(math.isfinite(x) and abs(x) < 1000000 for x in atlas1)
        if not atlas1_valid:
            atlas1 = (0.0, 1.0, 0.0, 1.0)
        lod_id = u32(b, h)
        found.append(dict(p=p,name=name.decode('ascii'),end=end,lod=lod_id,
                          section_header_offset=h, section_name_offset=p-4,
                          max_index=max(valid),indices=indices,atlas=atlas,atlas_valid=atlas_valid,
                          atlas1=atlas1,atlas1_valid=atlas1_valid))
    return found

def find_part_name(b, first_section_start, previous_vertex_end):
    # Provisional naming: last length-prefixed component label before first material.
    # Exclude known material section name and common structural labels.
    hits=[]
    for p in range(max(previous_vertex_end,first_section_start-700),first_section_start-4):
        n=u32(b,p)
        if not 3<=n<=48 or p+4+n>first_section_start: continue
        raw=b[p+4:p+4+n]
        if all((65<=c<=90 or 97<=c<=122 or 48<=c<=57 or c==95) for c in raw):
            hits.append((p,raw.decode('ascii')))
    return hits[-1][1] if hits else 'unknown_0x%X'%first_section_start

def parse(path):
    b=Path(path).read_bytes()
    version = u32(b, 0) if len(b) >= 4 else -1
    if version not in (1, 5):
        raise ValueError('Unsupported CARBIN version %s (expected 1 or 5)' % version)
    index_width = 2 if version == 1 else 4
    sections=find_sections(b, index_width)
    blocks=[]; previous_end=0; cursor=0
    for j, sec in enumerate(sections):
        end=sec['end']
        if end+20>len(b): continue
        marker,kind,count,stride,flag=struct.unpack_from('>5I',b,end)
        if (marker,kind,stride,flag)!=(0,3,28,1) or not 1<=count<=100000: continue
        vertex_start=end+20; vertex_end=vertex_start+count*28
        if vertex_end>len(b): continue
        group=sections[cursor:j+1]
        if not group or any(s['max_index']>=count for s in group):
            cursor=j+1; previous_end=vertex_end; continue
        label=find_part_name(b,group[0]['p'],previous_end)
        vertices=[]; uv0=[]; uv1=[]; normals=[]
        for k in range(count):
            at=vertex_start+k*28
            x,y,z,w=struct.unpack_from('>4h',b,at+4)
            vertices.append((x*w/32767.0**2,y*w/32767.0**2,z*w/32767.0**2))
            a,c,d,e=struct.unpack_from('>4H',b,at+12)
            uv0.append((a/65535.,c/65535.)); uv1.append((d/65535.,e/65535.))
            qx,qy,qz,qw=[v/32767. for v in struct.unpack_from('>4h',b,at+20)]
            nx=1-2*(qy*qy+qz*qz); ny=2*(qx*qy+qz*qw); nz=2*(qx*qz-qy*qw)
            mag=math.sqrt(nx*nx+ny*ny+nz*nz)
            normals.append((nx/mag,ny/mag,nz/mag) if mag>1e-10 else (1.,0.,0.))
        blocks.append(dict(label=label,offset=end,count=count,sections=group,
                           vertices=vertices,uv0=uv0,uv1=uv1,normals=normals))
        cursor=j+1; previous_end=vertex_end
    return blocks, len(sections)


def resolve_native_bounds(data, block):
    """Read 9-float position metadata preceding the first material section.

    v0.8-compatible remapping, without OBJ or calibration files.
    Reject ambiguous or absent headers instead of silently misplacing geometry.
    """
    sec = block['sections'][0]['p']
    raw = block['vertices']
    lo = [min(v[k] for v in raw) for k in range(3)]
    hi = [max(v[k] for v in raw) for k in range(3)]
    if any(hi[k] - lo[k] < 1e-12 for k in range(3)):
        raise ValueError('Degenerate packed bounds')
    candidates = []
    for at in range(max(0, sec-1200), sec-36):
        vals = struct.unpack_from('>9f', data, at)
        if not all(math.isfinite(v) and abs(v) < 30 for v in vals):
            continue
        if not all(1e-7 < vals[k+6]-vals[k+3] < 15 for k in range(3)):
            continue
        scales = [(vals[k+6]-vals[k+3])/(hi[k]-lo[k]) for k in range(3)]
        if not all(0.00001 < x < 100 for x in scales):
            continue
        # Position metadata describes a nearly uniform scale for packed positions.
        # Reject accidental float sequences that don't meet that constraint.
        mean = sum(scales)/3
        spread = max(abs(x-mean) for x in scales)/mean
        if spread > 0.003:
            continue
        candidates.append((at, vals, spread))
    if not candidates:
        raise ValueError('No valid native position header preceding %s at 0x%X' % (block['label'], sec))
    # The header belongs to the nearby component, not earlier geometry.
    at, vals, spread = max(candidates, key=lambda x: x[0])
    return at, vals, spread


def import_blender(path):
    import bpy
    from mathutils import Matrix, Vector
    path = Path(path)
    data = path.read_bytes()
    version = u32(data, 0)
    blocks, section_count = parse(path)
    source_digest = hashlib.sha256(data).hexdigest()
    # FH1 LOD0 and cockpit section headers explicitly encode LOD ID 0.
    # Reject unexpected IDs rather than silently mislabelling export metadata.
    unexpected = [(s['name'], s['lod'], s['section_header_offset'])
                  for block in blocks for s in block['sections'] if s['lod'] != 0]
    if unexpected:
        raise ValueError('Unexpected LOD IDs in LOD0/cockpit file: %s' % unexpected[:8])
    root = shared_root(bpy, path)
    is_cockpit = bool(re.search(r'(?i)_cockpit$', path.stem))
    is_brake_part = version == 1 and bool(re.search(r'(?i)_(?:rotor|caliper)[A-Za-z0-9]*_lod0$', path.stem))
    group_name = 'Cockpit' if is_cockpit else ('Brake Parts' if is_brake_part else 'LOD0')
    suffix = '_C' if is_cockpit else '_L0'
    lod = child_collection(bpy, root, group_name)
    source_role = 'cockpit' if is_cockpit else ('brake_part_lod0' if is_brake_part else 'exterior_lod0')
    lod['fh1_source_role'] = source_role
    lod['fh1_source_carbin'] = path.name
    # Replace only meshes from the same source role; never clear the other group.
    for old_node in list(lod.children):
        if old_node.get('fh1_source_carbin') == path.name or (
            not is_brake_part and (old_node.get('fh1_lod0_component') or old_node.get('fh1_cockpit_component'))
        ):
            for obj in list(old_node.objects):
                bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.collections.remove(old_node)
    basis = Matrix.Rotation(math.pi/2,3,'X')
    created=0; failures=[]
    # Match v0.8: case-insensitive alphabetical component ordering, _L0 suffix.
    # The parser's source order is retained in custom properties for future
    # scene-graph/export work; no component transforms or UVs are changed here.
    seen_names = set()
    # Share Blender materials by actual shader identity across LOD0 and cockpit.
    # Native CARBIN section identity remains on each face independently.
    source_materials = {}
    def section_material(sec, section_idx):
        shader_name = sec['name']
        if shader_name in source_materials:
            return source_materials[shader_name]
        # Prefer a material already imported from either LOD0 or cockpit.
        # Never merge into a manually duplicated/custom material instance.
        for candidate in bpy.data.materials:
            if (candidate.get('fh1_shader_name') == shader_name
                    and candidate.get('fh1_native_shared_shader') is True):
                source_materials[shader_name] = candidate
                return candidate
        prefix = 'FH1_' + shader_name
        existing = bpy.data.materials.get(prefix)
        if existing is not None and (existing.get('fh1_shader_name', shader_name) == shader_name
                                    and not existing.get('fh1_custom_material_instance', False)):
            existing['fh1_native_shared_shader'] = True
            source_materials[shader_name] = existing
            return existing
        number = 2
        while bpy.data.materials.get(prefix + str(number)) is not None:
            number += 1
        material = bpy.data.materials.new(prefix if existing is None else prefix + str(number))
        material['fh1_shader_name'] = shader_name
        material['fh1_native_shared_shader'] = True
        source_materials[shader_name] = material
        return material
    for block in sorted(blocks, key=lambda x: (x['label'].casefold(), x['offset'])):
        try:
            header_at, vals, spread = resolve_native_bounds(data,block)
            trans, dmin, dmax = vals[:3], vals[3:6], vals[6:9]
            raw = block['vertices']
            low = [min(v[k] for v in raw) for k in range(3)]
            high = [max(v[k] for v in raw) for k in range(3)]
            def native_position(i):
                p=raw[i]
                return Vector(tuple(trans[k]+dmin[k]+(p[k]-low[k])/(high[k]-low[k])*(dmax[k]-dmin[k]) for k in range(3)))
            facepairs=[(s,t) for s in block['sections'] for t in faces_from_strip(s['indices'], RESTART_16 if version == 1 else RESTART)]
            if not facepairs:continue
            used=sorted({i for s,t in facepairs for i in t})
            remap={idx:j for j,idx in enumerate(used)}
            verts=[tuple(basis@native_position(i)) for i in used]
            part=block['label']
            mesh=bpy.data.meshes.new(part+suffix+'_mesh')
            mesh.from_pydata(verts,[],[tuple(remap[i] for i in tri) for sec,tri in facepairs]);mesh.update()
            # Dev 12: preserve exact source vertex identity for future native
            # LOD0/cockpit writing. Zero is reserved for newly created vertices.
            vertex_identity = mesh.attributes.new(name='fh1_original_vertex_id', type='INT', domain='POINT')
            for local_id, source_id in enumerate(used):
                vertex_identity.data[local_id].value = source_id + 1
            face_identity = mesh.attributes.new(name='fh1_original_section_id', type='INT', domain='FACE')
            for face_id, (source_section, _) in enumerate(facepairs):
                face_identity.data[face_id].value = block['sections'].index(source_section) + 1

            # Preserve original CARBIN section membership per polygon, independently
            # of material slots (multiple sections can share a material).
            section_index_by_id = {id(sec): index for index, sec in enumerate(block['sections'])}
            section_attribute = mesh.attributes.new(name='fh1_section_index', type='INT', domain='FACE')
            for face_index, (sec, _) in enumerate(facepairs):
                section_attribute.data[face_index].value = section_index_by_id[id(sec)]
            # A collection represents the component node; its single mesh
            # retains all material sections as slots, not separate objects.
            node_name = part + suffix
            if node_name in seen_names:
                node_name = '%s%s_0x%X' % (part, suffix, block['offset'])
            seen_names.add(node_name)
            node = bpy.data.collections.new(node_name)
            node['fh1_lod0_component'] = not is_cockpit and not is_brake_part
            node['fh1_brake_part_component'] = is_brake_part
            node['fh1_source_carbin'] = path.name
            node['fh1_cockpit_component'] = is_cockpit
            node['fh1_source_role'] = source_role
            lod.children.link(node)
            node['fh1_component_name'] = part
            node['fh1_source_vertex_header_offset'] = block['offset']
            node['fh1_hierarchy_status'] = 'Mesh component; scene-graph parentage not yet decoded'
            obj=bpy.data.objects.new(node_name,mesh)
            node.objects.link(obj)
            # Preserve distinct CARBIN section records with clean, numbered names.
            # Identical source records encountered again reuse their material.
            slot_by_shader = {}
            section_to_slot = {}
            for section_idx, sec in enumerate(block['sections']):
                material = section_material(sec, section_idx)
                key = material.as_pointer()
                if key not in slot_by_shader:
                    slot_by_shader[key] = len(mesh.materials)
                    mesh.materials.append(material)
                section_to_slot[section_idx] = slot_by_shader[key]
            uv0=mesh.uv_layers.new(name='UV0');uv1=mesh.uv_layers.new(name='UV1')
            for poly,(sec,_) in zip(mesh.polygons,facepairs):
                poly.material_index=section_to_slot[section_index_by_id[id(sec)]];poly.use_smooth=True
                uoff,uscale,voff,vscale=sec['atlas']
                u1off,u1scale,v1off,v1scale=sec['atlas1']
                for li in poly.loop_indices:
                    idx=used[mesh.loops[li].vertex_index]
                    u,v=block['uv0'][idx]
                    uv0.data[li].uv=(u*uscale+uoff,1-(v*vscale+voff))
                    u1,v1=block['uv1'][idx]
                    uv1.data[li].uv=(u1*u1scale+u1off,1-(v1*v1scale+v1off))
            mesh.use_auto_smooth=True
            converted=[tuple((basis@Vector(block['normals'][i])).normalized()) for i in used]
            mesh.normals_split_custom_set([converted[l.vertex_index] for l in mesh.loops]);mesh.update()
            obj['fh1_uv1_atlas_provisional_materials'] = ','.join(s['name'] for s in block['sections'] if not s.get('atlas1_valid',True))
            obj['fh1_uv_atlas_provisional_materials'] = ','.join(s['name'] for s in block['sections'] if not s.get('atlas_valid',True))
            # Source fingerprint and native offsets for fail-closed export validation.
            obj['fh1_export_source_path'] = str(path.resolve())
            obj['fh1_export_source_sha256'] = source_digest
            obj['fh1_imported_vertex_count'] = len(mesh.vertices)
            obj['fh1_imported_face_count'] = len(mesh.polygons)
            obj['fh1_native_vertex_data_offset'] = block['offset'] + 20
            obj['fh1_native_vertex_count'] = block['count']
            obj['fh1_native_vertex_stride'] = 28
            obj['fh1_native_vertex_format'] = 'post-section-28-byte'
            obj['fh1_native_carbin']=path.name
            obj['fh1_source_role']=source_role
            obj['fh1_carbin_version']=version
            obj['fh1_source_index_width']=2 if version == 1 else 4
            obj['fh1_source_component_order']=next((i for i, x in enumerate(blocks) if x is block), -1)
            obj['fh1_transform_source']='Native 9-float position header (v0.8 bounds remapping)'
            obj['fh1_position_header_offset']=header_at
            obj['fh1_position_header_values']=list(vals)
            obj['fh1_scale_uniformity_error']=spread
            obj['fh1_vertex_header']=hex(block['offset'])
            # Explicit, original source section metadata (not derived from Blender materials).
            # Section order is within the source component's material/index section list.
            obj['fh1_section_offsets'] = [s['section_name_offset'] for s in block['sections']]
            obj['fh1_source_section_header_offsets'] = [s['section_header_offset'] for s in block['sections']]
            obj['fh1_source_section_lod_ids'] = [s['lod'] for s in block['sections']]
            obj['fh1_source_section_indices'] = list(range(len(block['sections'])))
            obj['fh1_section_materials'] = ','.join(s['name'] for s in block['sections'])
            obj['fh1_all_section_count'] = len(block['sections'])
            obj['fh1_imported_section_count'] = len(block['sections'])
            obj['fh1_lod_boundary_method'] = 'explicit section header LOD ID'
            obj['fh1_lod_confirmed'] = True
            obj['fh1_section_face_attribute'] = 'fh1_section_index'
            created+=1
        except Exception as exc:
            failures.append((block['label'],str(exc)))
            print('[FH1 LOD0 native] SKIPPED',block['label'],exc)
    print('[FH1 LOD0 hierarchy] Imported %d / %d blocks, %d sections; %d skipped' % (created,len(blocks),section_count,len(failures)))
    if failures:
        print('[FH1 LOD0 native] Missing:',failures)
    return created, failures

def choose_file():
    import bpy
    from bpy.props import StringProperty
    from bpy_extras.io_utils import ImportHelper
    class FH1_OT_lod0_cockpit_v014(bpy.types.Operator,ImportHelper):
        bl_idname='import_scene.fh1_lod0_cockpit_v014'
        bl_label='Import FH1 LOD0 / Cockpit v0.15.3 (Cleanup, Section Face Metadata)'
        filename_ext='.carbin'
        filter_glob:StringProperty(default='*.carbin',options={'HIDDEN'})
        def execute(self,context):
            try:import_blender(self.filepath)
            except Exception as e:
                self.report({'ERROR'},str(e));raise
            return {'FINISHED'}
    try:
        bpy.utils.register_class(FH1_OT_lod0_cockpit_v014)
    except ValueError:
        old = bpy.types.Operator.bl_rna_get_subclass_py('FH1_OT_lod0_cockpit_v014', None)
        if old is None: raise
        bpy.utils.unregister_class(old)
        bpy.utils.register_class(FH1_OT_lod0_cockpit_v014)
    bpy.ops.import_scene.fh1_lod0_cockpit_v014('INVOKE_DEFAULT')

if __name__=='__main__':choose_file()
