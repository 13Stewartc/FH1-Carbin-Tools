"""LOD1-5 joined geometry writer for added faces and edited vertex positions.

Writes a NEW file; does not overwrite the source. Structural parser checks are
not equivalent to a game load test. Back up your .blend before testing.
"""
import hashlib
import math
import struct
from pathlib import Path
from . import import_lod, topology_plan, export_positions, lod_isolation, damage_donor

VERSION = '0.4.0-dev5'


def _normal_bytes(normal):
    # FH1 stores quaternion rotation of the X axis, not XYZ float normals.
    from mathutils import Vector
    n = Vector(normal)
    if n.length < 1e-8:
        raise ValueError('Zero normal in new geometry')
    q = Vector((1.0, 0.0, 0.0)).rotation_difference(n.normalized())
    return struct.pack('>4h', *(max(-32767,min(32767,round(x*32767))) for x in (q.x,q.y,q.z,q.w)))


def _vertex_data(obj, rec, sections, new_indices, plan, duplicate_start):
    from mathutils import Matrix
    mesh=obj.data
    mesh.calc_normals_split()  # Blender 3.6: preserves authored custom split normals
    inv=Matrix.Rotation(-math.pi/2,3,'X')
    uv0=mesh.uv_layers.get('UV0');uv1=mesh.uv_layers.get('UV1')
    if uv0 is None or uv1 is None: raise ValueError('Both UV0 and UV1 are required')
    section_map=list(obj.get('fh1_source_section_indices',[]))
    variants={};attrs={};extra=[];triangles={str(k):[] for k in plan['new_triangles_by_source_section']}
    next_id=duplicate_start
    source_by_vi={vi:idx for vi,idx in new_indices.items()}
    for poly in mesh.polygons:
        if mesh.attributes['fh1_original_section_id'].data[poly.index].value>0: continue
        si=section_map[poly.material_index];sec=sections[si]
        uoff,uscale,voff,vscale=sec['uv']
        if abs(uscale)<1e-9 or abs(vscale)<1e-9:raise ValueError('Zero UV atlas scale')
        tri=[]
        for li in poly.loop_indices:
            vi=mesh.loops[li].vertex_index
            if vi not in source_by_vi:raise ValueError('New face references original vertex; unsupported normal split')
            u,v=uv0.data[li].uv;c,d=uv1.data[li].uv
            values=((u-uoff)/uscale,((1-v)-voff)/vscale,c,d)
            if any(not math.isfinite(x) or x < -1e-5 or x>1+1e-5 for x in values):
                raise ValueError('UV out of range at face %d loop %d'%(poly.index,li))
            packed=[max(0,min(65535,round(x*65535))) for x in values]
            normal=inv @ mesh.loops[li].normal
            encoded=_normal_bytes(normal)
            # Quantization is tested against the actual decoder's first row.
            qx,qy,qz,qw=[x/32767 for x in struct.unpack('>4h',encoded)]
            decoded=(1-2*(qy*qy+qz*qz),2*(qx*qy+qz*qw),2*(qx*qz-qy*qw))
            dot=sum(x*y for x,y in zip(normal.normalized(),decoded))/max(1e-12,math.sqrt(sum(x*x for x in decoded)))
            if dot<0.9999:raise ValueError('Normal quaternion round-trip failed at face %d (dot %.6f)'%(poly.index,dot))
            data=struct.pack('>4H',*packed)+encoded
            key=(vi,data)
            if key not in variants:
                existing=source_by_vi[vi]
                if not any(k[0]==vi for k in variants): idx=existing
                else:
                    idx=next_id;next_id+=1
                    if idx>=65535:raise ValueError('16-bit index limit exceeded')
                    extra.append((vi,idx,data))
                variants[key]=idx;attrs[idx]=data
            tri.append(variants[key])
        if len(tri)!=3:raise ValueError('Triangulate new geometry before export')
        triangles[str(si)].append(tri)
    if not set(new_indices).issubset({k[0] for k in variants}):raise ValueError('Unreferenced new vertex')
    return attrs,extra,triangles


def _expand_atlas_for_new_uvs(obj, original, record, plan):
    """Expand section atlas while retaining authored UVs and existing section UVs.

    Existing packed UV0 values are rewritten only if all uses of each shared
    vertex agree on the replacement encoding. Reject conflicting shared uses.
    """
    mesh = obj.data
    uv = mesh.uv_layers.get('UV0')
    if uv is None:
        raise ValueError('UV0 missing')
    ids = mesh.attributes['fh1_original_vertex_id']
    section_ids = list(obj.get('fh1_source_section_indices', []))
    additions = plan['new_triangles_by_source_section']
    changed = {}
    for key, tris in additions.items():
        if not tris:
            continue
        si = int(key)
        sec = record['sections'][si]
        uoff, uscale, voff, vscale = sec['uv']
        target = []
        for poly in mesh.polygons:
            if ids is None or poly.material_index >= len(section_ids) or section_ids[poly.material_index] != si:
                continue
            for li in poly.loop_indices:
                if ids.data[mesh.loops[li].vertex_index].value == 0:
                    u, v = uv.data[li].uv
                    if not math.isfinite(u) or not math.isfinite(v):
                        raise ValueError('Non-finite UV0 on new geometry')
                    target.append((float(u), 1.0-float(v)))
        if not target:
            continue
        umin = min(uoff, uoff+uscale, *(x for x,y in target))
        umax = max(uoff, uoff+uscale, *(x for x,y in target))
        vmin = min(voff, voff+vscale, *(y for x,y in target))
        vmax = max(voff, voff+vscale, *(y for x,y in target))
        if abs(umax-umin)<1e-9 or abs(vmax-vmin)<1e-9:
            raise ValueError('Degenerate expanded UV atlas')
        changed[si] = (umin, umax-umin, vmin, vmax-vmin)
    if not changed:
        return original, record['sections'], 0
    # Build the complete shared-vertex usage map, including other LODs.
    # A single packed UV cannot represent two different section-local values.
    use = {}
    for si, sec in enumerate(record['sections']):
        for tri in sec['faces']:
            for idx in tri:
                use.setdefault(idx, set()).add(si)
    proposed = {}
    duplicates = []
    remaps = {}
    next_index = record['count'] + len(plan['new_vertices'])
    for idx, used in sorted(use.items()):
        if idx >= record['count']:
            continue
        a,b = struct.unpack_from('>2H', original, record['start']+idx*28+8)
        candidates = {}
        for si in sorted(used):
            old = record['sections'][si]['uv']
            new = changed.get(si, old)
            u = old[0]+a/65535.0*old[1]
            v = old[2]+b/65535.0*old[3]
            nu=(u-new[0])/new[1]
            nv=(v-new[2])/new[3]
            if not (-1e-6<=nu<=1+1e-6 and -1e-6<=nv<=1+1e-6):
                raise ValueError('Expanded UV atlas out of range for source vertex %d'%idx)
            candidates[si] = tuple(max(0,min(65535,round(x*65535))) for x in (nu,nv))
        if not any(si in changed for si in used):
            continue
        # Keep the original vertex for the most common encoding; duplicate
        # for every distinct section-specific UV encoding.
        groups = {}
        for si, encoding in candidates.items():
            groups.setdefault(encoding, []).append(si)
        primary = max(groups, key=lambda enc: (len(groups[enc]), -min(groups[enc])))
        proposed[idx] = primary
        for encoding, sections_using in groups.items():
            if encoding == primary:
                continue
            if next_index >= 65535:
                raise ValueError('16-bit vertex index capacity exceeded')
            duplicates.append((idx, next_index, encoding))
            for si in sections_using:
                remaps[(si,idx)] = next_index
            next_index += 1
    updated=bytearray(original)
    for si, atlas in changed.items():
        off=record['sections'][si]['offset']-81+13
        struct.pack_into('>4f', updated, off, *atlas)
    for idx,(a,b) in proposed.items():
        struct.pack_into('>2H',updated,record['start']+idx*28+8,a,b)
    # Rewrite existing index values IN PLACE; indices are section-local strips
    # and retain their original restart markers and lengths.
    for (si,idx), replacement in remaps.items():
        off=record['sections'][si]['offset']
        n=struct.unpack_from('>I',original,off)[0]
        hdr=off+4+n
        count=struct.unpack_from('>I',original,hdr+72)[0]
        for k in range(count):
            pos=hdr+80+k*2
            if struct.unpack_from('>H',original,pos)[0]==idx:
                struct.pack_into('>H',updated,pos,replacement)
    extra=[]
    for idx, replacement, encoding in duplicates:
        raw=bytearray(updated[record['start']+idx*28:record['start']+(idx+1)*28])
        struct.pack_into('>2H',raw,8,*encoding)
        extra.append((idx,replacement,bytes(raw)))
    sections=[dict(sec, uv=changed.get(si,sec['uv'])) for si,sec in enumerate(record['sections'])]
    return bytes(updated),sections,extra


def export_joined(obj, destination):
    from mathutils import Matrix
    plan=topology_plan.build_plan(obj)
    source=Path(plan['source']).resolve()
    dest=Path(destination).resolve()
    if dest==source: raise ValueError('Refusing to overwrite source CARBIN')
    original=source.read_bytes()
    if hashlib.sha256(original).hexdigest()!=plan['sha256']:
        raise ValueError('Source file checksum mismatch')
    records=import_lod.discover(original)
    record=next((r for r in records if r['p']==plan['record_offset']),None)
    if record is None: raise ValueError('Original record missing')
    # Fail closed if original geometry has been deleted or its source identity
    # is ambiguous. Rebuilding deleted original strips is not supported yet.
    orig_ids=obj.data.attributes['fh1_original_vertex_id']
    original_ids=[a.value-1 for a in orig_ids.data if a.value>0]
    if len(original_ids)!=len(set(original_ids)):
        raise ValueError('Duplicate original vertex provenance')
    face_attr=obj.data.attributes['fh1_original_section_id']
    old_face_count=sum(a.value>0 for a in face_attr.data)
    source_faces=sum(len(s['faces']) for s in record['sections'] if s['lod']==plan['lod'])
    if old_face_count!=source_faces:
        raise ValueError('Original faces removed or changed; this exporter only appends geometry')
    section_ids=list(obj.get('fh1_source_section_indices',[]))
    new_ids={v['blender_vertex']:v['carbin_vertex'] for v in plan['new_vertices']}
    original, updated_sections, duplicated_vertices = _expand_atlas_for_new_uvs(obj,original,record,plan)
    attrs, normal_duplicates, new_triangles = _vertex_data(obj,record,updated_sections,new_ids,plan,record['count']+len(new_ids)+len(duplicated_vertices))
    prev=0
    for r in records:
        h,vals,_=import_lod.resolve_position_header(original,r,prev)
        r['position_header_offset']=h;r['position_header']=vals
        r['source_positions']=import_lod.decode_vertices(original,r)[0]
        prev=r['end']
    # A shared header or record would also affect other LODs. For now,
    # fail rather than risk altering unrelated geometry.
    if any(r is not record and r['position_header_offset']==record['position_header_offset'] for r in records):
        raise ValueError('Shared position header: unsafe to expand this buffer')
    mesh=obj.data
    inv=Matrix.Rotation(-math.pi/2,3,'X')
    desired={v['carbin_vertex']:tuple(inv @ mesh.vertices[v['blender_vertex']].co) for v in plan['new_vertices']}
    # Preserve provenance of imported vertices even after Ctrl+J. This allows
    # position edits to existing mesh geometry in the SAME export as additions.
    # The source buffer may contain vertices shared by other LODs; never
    # duplicate them or silently discard a position edit.
    existing_changes=0
    for vi, attr in enumerate(orig_ids.data):
        source_id=int(attr.value)
        if source_id <= 0:
            continue
        idx=source_id-1
        if idx >= record['count']:
            raise ValueError('Original vertex %d exceeds source buffer' % idx)
        pos=tuple(inv @ mesh.vertices[vi].co)
        old=record['source_positions'][idx]
        if max(abs(pos[k]-old[k]) for k in range(3)) > lod_isolation.POSITION_CHANGE_EPSILON:
            desired[idx]=pos
            existing_changes+=1
    # Dev 9: isolate edited source vertices that are referenced by another LOD.
    shared = lod_isolation.shared_lod_vertices(record, plan['lod'])
    isolated = {}
    next_isolated = record['count'] + len(new_ids) + len(duplicated_vertices) + len(normal_duplicates)
    for idx in sorted(set(desired) & shared):
        if next_isolated >= 65535:
            raise ValueError('16-bit vertex capacity exceeded during LOD isolation')
        isolated[idx] = next_isolated
        next_isolated += 1
    # The original source IDs must retain their old positions for other LODs.
    for idx, new_idx in isolated.items():
        desired[new_idx] = desired.pop(idx)
    # An untouched source vertex stays at its original decoded coordinate.

    # Preserve exact original 28-byte records. New records use existing
    # tail bytes as a template (unknown flags), overriding known fields.
    exemplar=original[record['start']:record['start']+28]
    extra=bytearray()
    for vi,idx in sorted(new_ids.items(),key=lambda kv:kv[1]):
        packed=bytearray(exemplar)
        struct.pack_into('>4h',packed,0,0,0,0,32767)
        packed[8:24]=attrs[idx]
        extra.extend(packed)
    # Duplicate original records after new Blender vertices, preserving
    # all packed position/normal/unknown fields verbatim.
    for old_idx, new_idx, raw in duplicated_vertices:
        if new_idx != record['count'] + len(new_ids) + (len(extra)-len(new_ids)*28)//28:
            raise ValueError('Noncontiguous duplicated vertex allocation')
        extra.extend(raw)
    for vi, idx, data in normal_duplicates:
        raw=bytearray(exemplar)
        struct.pack_into('>4h',raw,0,0,0,0,32767)
        raw[8:24]=data
        extra.extend(raw)
        desired[idx]=tuple(inv @ mesh.vertices[vi].co)
    # Copy the complete original 28-byte record, including UVs, normals and
    # unknown bytes. Position repacking below updates its XYZ only.
    for old_idx, new_idx in isolated.items():
        extra.extend(original[record['start']+old_idx*28:record['start']+(old_idx+1)*28])
    inserts=[(record['end'],bytes(extra))]
    replacements=[]
    # Insert a strip restart then each new triangle with its own restart.
    for si,triangles in new_triangles.items():
        if not triangles:continue
        sec=record['sections'][int(si)]
        off=sec['offset'];name_len=struct.unpack_from('>I',original,off)[0]
        hdr=off+4+name_len
        ic=struct.unpack_from('>I',original,hdr+72)[0]
        insertion=hdr+80+ic*2
        strip=[65535]
        for tri in triangles:
            strip.extend(tri);strip.append(65535)
        # Last restart is harmless but remove to minimize padding.
        strip.pop()
        addition=struct.pack('>'+str(len(strip))+'H',*strip)
        inserts.append((insertion,addition))
        replacements.append((hdr+72,ic+len(strip)))
    # Rewrite only the selected LOD's existing strip indices, in place.
    # Do not decode/re-encode strips: this preserves restart markers, parity,
    # degenerate indices, triangle winding and exact section byte lengths.
    remapped_source = bytearray(original)
    for sec in record['sections']:
        if sec['lod'] != plan['lod']:
            continue
        off = sec['offset']
        name_len = struct.unpack_from('>I', original, off)[0]
        hdr = off + 4 + name_len
        count = struct.unpack_from('>I', original, hdr+72)[0]
        for k in range(count):
            index_off = hdr + 80 + k*2
            old_idx = struct.unpack_from('>H', original, index_off)[0]
            if old_idx in isolated:
                struct.pack_into('>H', remapped_source, index_off, isolated[old_idx])
    original = bytes(remapped_source)
    # Dev17 LOD1-5 experiment: extend the matching secondary 4-byte stream.
    # It is located AFTER the geometry section(s), not directly after the
    # 28-byte vertex records. Its old count is otherwise left unchanged.
    added_count = len(new_ids) + len(duplicated_vertices) + len(normal_duplicates) + len(isolated)
    if added_count:
        stream_signature = struct.pack('>4I', 3, record['count'], 4, 1)
        record_index = next(i for i, r in enumerate(records) if r['p'] == record['p'])
        search_end = records[record_index + 1]['p'] if record_index + 1 < len(records) else len(original)
        candidates = []
        search_pos = record['end']
        while True:
            found = original.find(stream_signature, search_pos, search_end)
            if found < 0:
                break
            stream_end = found + 16 + record['count'] * 4
            if stream_end <= search_end:
                candidates.append((found, stream_end))
            search_pos = found + 1
        if len(candidates) != 1:
            raise ValueError('Dev17: expected exactly one matching secondary stream for %s; found %d' % (record['name'], len(candidates)))
        stream_header, stream_end = candidates[0]
        # New vertices get neutral records; all source records stay byte-identical.
        # This tests the stream-count hypothesis, NOT confirmed damage semantics.
        # DEV17 EXACT DEFAULT: append neutral four-byte records.
        secondary_payload = b'\x00' * (4 * added_count)
        if damage_donor.enabled():
            source_records = [original[stream_header+16+i*4:stream_header+20+i*4] for i in range(record['count'])]
            target_positions = (
                [desired[idx] for _,idx in sorted(new_ids.items(),key=lambda kv:kv[1])]
                + [record['source_positions'][old_idx] for old_idx,_,_ in duplicated_vertices]
                + [desired[idx] for _,idx,_ in normal_duplicates]
                + [desired[idx] for _,idx in isolated.items()]
            )
            if len(target_positions) != added_count:
                raise ValueError('Dev18 secondary donor vertex allocation mismatch')
            donor_positions = record['source_positions']
            if damage_donor.donor_mode() not in ('SAME', 'OFF'):
                donor_key = damage_donor.donor_mode()
                donor_name = donor_key[2:] if donor_key.startswith('P:') else ''
                donor = next((r for r in records if r['name'] == donor_name), None)
                if donor is None:
                    raise ValueError('Selected deformation donor not found in source CARBIN: ' + donor_key)
                donor_idx = records.index(donor)
                donor_bound = records[donor_idx+1]['p'] if donor_idx+1 < len(records) else len(original)
                donor_sig = struct.pack('>4I', 3, donor['count'], 4, 1)
                donor_hits = []
                scan = donor['end']
                while True:
                    hit = original.find(donor_sig, scan, donor_bound)
                    if hit < 0: break
                    if hit + 16 + donor['count']*4 <= donor_bound:
                        donor_hits.append(hit)
                    scan = hit + 1
                if len(donor_hits) != 1:
                    raise ValueError('Dev18 rear bumper secondary stream ambiguous: %d matches' % len(donor_hits))
                donor_start = donor_hits[0] + 16
                source_records = [original[donor_start+i*4:donor_start+(i+1)*4] for i in range(donor['count'])]
                donor_positions = donor['source_positions']
                print('[FH1 Dev18] Using rear bumper donor:', donor['name'], 'for', record['name'])
            secondary_payload = damage_donor.nearest_records(donor_positions, source_records, target_positions)
            print('[FH1 Dev18] Nearest-source secondary records copied for', added_count, 'vertices in', record['name'])
        inserts.append((stream_end, secondary_payload))
        replacements.append((stream_header + 4, record['count'] + added_count))
    # Insert from end to start; adjust original fixed-position fields first.
    result=bytearray(original)
    for off,value in replacements:
        struct.pack_into('>I',result,off,value)
    struct.pack_into('>I',result,record['start']-16,record['count']+len(new_ids)+len(duplicated_vertices)+len(normal_duplicates)+len(isolated))
    for off,payload in sorted(inserts,reverse=True):result[off:off]=payload
    def shifted(pos):return pos+sum(len(blob) for off,blob in inserts if off<=pos)
    expanded=dict(record)
    expanded['p']=shifted(record['p'])
    expanded['start']=shifted(record['start'])
    expanded['count']=record['count']+len(new_ids)+len(duplicated_vertices)+len(normal_duplicates)+len(isolated)
    expanded['position_header_offset']=shifted(record['position_header_offset'])
    expanded['source_positions']=record['source_positions']+[desired[i] for i in range(record['count'],record['count']+len(new_ids))]+[record['source_positions'][old_idx] for old_idx,_,_ in duplicated_vertices]+[desired[idx] for _,idx,_ in normal_duplicates]+[record['source_positions'][old_idx] for old_idx in isolated]
    if len(expanded['source_positions'])!=expanded['count']:
        raise ValueError('New vertex allocation is not contiguous')
    # Duplicate positions are identical to their source originals. Repack
    # includes them in the expanded position bounds and count.
    desired.update({new_idx: record['source_positions'][old_idx] for old_idx,new_idx,_ in duplicated_vertices})
    # Repack all coordinates and update the position header using v0.2.7.
    err=export_positions.repack_record(result,bytes(result),expanded,desired)
    parsed=import_lod.discover(result)
    rebuilt=next((r for r in parsed if r['p']==expanded['p']),None)
    if rebuilt is None or rebuilt['count']!=expanded['count']:
        raise ValueError('Rebuilt CARBIN record failed independent parser check')
    expected_add=sum(map(len,new_triangles.values()))
    actual_add=sum(len(s['faces']) for s in rebuilt['sections'] if s['lod']==plan['lod'])-source_faces
    if actual_add!=expected_add:
        raise ValueError('Triangle count mismatch: expected %d, found %d'%(expected_add,actual_add))
    # All other records must still be discoverable with original counts.
    if len(parsed)!=len(records):
        raise ValueError('Original record count changed unexpectedly')
    for old,new in zip(records,parsed):
        if old is record:continue
        if old['count']!=new['count'] or len(old['sections'])!=len(new['sections']):
            raise ValueError('Untouched record changed: '+old['name'])
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_bytes(result)
    return len(new_ids),expected_add,err,len(result),existing_changes
