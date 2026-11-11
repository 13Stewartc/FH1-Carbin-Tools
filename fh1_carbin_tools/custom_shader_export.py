"""Dev92 experimental native section injection for custom shaders.

Native geometry writers encode new vertices using an existing section's UV atlas.
This module moves the resulting new triangle strips into cloned, independently
named native sections. It does not claim FXOBJ texture bindings are validated.
"""
import struct
from pathlib import Path
from . import import_lod, import_lod0


def _shader(material):
    return str(material.get('fh1_custom_native_shader', '')).strip() if material else ''


def _donor(sections, lod=None):
    options = [(i,s) for i,s in enumerate(sections) if lod is None or s['lod'] == lod]
    if not options:
        raise ValueError('No original section available as UV layout donor')
    return next((i for i,s in options if s['name'].lower() in ('misc','black','rubber_trim')), options[0][0])


class Experiment:
    def __init__(self, selected, source, lod0):
        self.source = Path(source)
        self.original = self.source.read_bytes()
        self.lod0 = lod0
        self.restore = []
        self.targets = []
        if lod0:
            blocks,_ = import_lod0.parse(self.source)
            objects = [o for o in __import__('bpy').data.objects if o.type == 'MESH'
                       and Path(str(o.get('fh1_export_source_path', ''))).resolve() == self.source.resolve()]
            if selected not in objects and selected.type == 'MESH':
                objects.append(selected)
            for obj in objects:
                offset = int(obj.get('fh1_native_vertex_data_offset', -1))-20
                block_id = next((i for i,b in enumerate(blocks) if b['offset']==offset), None)
                if block_id is None:
                    if any(_shader(m) for m in obj.data.materials if m):
                        raise ValueError('Dev94: cannot match '+obj.name+' to native LOD0 source component; shader export aborted')
                    continue
                sections = blocks[block_id]['sections']
                donor = _donor(sections)
                self._collect(obj, block_id, sections, donor, lod0=True)
        else:
            records=import_lod.discover(self.original)
            rec_id=next((i for i,r in enumerate(records) if r['p']==int(selected.get('fh1_source_record_offset',-1))),None)
            if rec_id is None: raise ValueError('Source record unavailable for custom shader export')
            sections=records[rec_id]['sections']
            donor=_donor(sections,int(selected['fh1_source_lod']))
            self._collect(selected,rec_id,sections,donor,lod0=False)

    def _collect(self,obj,record_id,sections,donor,lod0):
        mesh=obj.data
        face_ids=mesh.attributes.get('fh1_original_section_id')
        if face_ids is None:
            if any(_shader(m) for m in mesh.materials if m):
                raise ValueError('Dev94: custom shader assigned but face provenance is missing on '+obj.name)
            return
        slots=list(obj.get('fh1_source_section_indices',[])) if not lod0 else None
        original_slots=list(slots) if slots is not None else None
        new_slots=False
        for poly in mesh.polygons:
            if face_ids.data[poly.index].value: continue
            if poly.material_index>=len(mesh.materials):continue
            mat=mesh.materials[poly.material_index]
            shader=_shader(mat)
            if not shader:continue
            if not shader.isascii() or not shader.replace('_','').isalnum() or not 2<=len(shader)<=48:
                raise ValueError('Invalid native shader name: '+shader)
            if any(sec['name'].lower()==shader.lower() for sec in sections):
                continue  # Native writer already knows this shader.
            if lod0:
                # Dev98: never mutate a shared Blender material. The native
                # writer resolves a donor per new face using custom metadata.
                pass
            else:
                while len(slots)<=poly.material_index:
                    slots.append(donor)
                slots[poly.material_index]=donor
                new_slots=True
            self.targets.append((record_id,donor,poly.index,shader,obj))
        if new_slots:
            self.restore.append(('mapping',obj,original_slots))
            obj['fh1_source_section_indices']=slots

    def close(self):
        for kind,item,old in reversed(self.restore):
            if kind=='mapping':item['fh1_source_section_indices']=old
            elif old is None:
                if 'fh1_shader_name' in item:del item['fh1_shader_name']
            else:item['fh1_shader_name']=old
        self.restore.clear()

    def finalize(self,destination):
        if not self.targets:
            # A normal export with no newly assigned custom shader must be untouched.
            # Do not run experimental section surgery on established geometry exports.
            return

        destination=Path(destination)
        data=destination.read_bytes()
        if self.lod0:
            orig,_=import_lod0.parse(self.source)
            parsed,_=import_lod0.parse(destination)
            if len(orig)!=len(parsed):raise ValueError('Dev92: LOD0 component count changed')
        else:
            orig=import_lod.discover(self.original)
            parsed=import_lod.discover(data)
            if len(orig)!=len(parsed):raise ValueError('Dev92: LOD1-5 component count changed')
        # Fail closed if the ordinary geometry writer has already lost components.
        # Section surgery must never conceal an incomplete baseline export.
        if self.lod0:
            orig_labels = [b['label'] for b in orig]
            new_labels = [b['label'] for b in parsed]
            if orig_labels != new_labels:
                raise ValueError('Dev96: baseline LOD0 component hierarchy differs before shader insertion; refusing to write a damaged CARBIN')
        else:
            orig_names = [r['name'] for r in orig]
            new_names = [r['name'] for r in parsed]
            if orig_names != new_names:
                raise ValueError('Dev96: baseline LOD1-5 component hierarchy differs before shader insertion')
        changes=[]
        for record_id in sorted({x[0] for x in self.targets}):
            oldsecs=orig[record_id]['sections'];newsecs=parsed[record_id]['sections']
            if len(oldsecs)!=len(newsecs):raise ValueError('Dev92: source section count changed unexpectedly')
            additions=[]
            for donor in sorted({x[1] for x in self.targets if x[0]==record_id}):
                source=oldsecs[donor];current=newsecs[donor]
                width=(2 if not self.lod0 else (2 if struct.unpack_from('>I',data,0)[0]==1 else 4))
                off=current['section_name_offset'] if self.lod0 else current['offset']
                hdr=current['section_header_offset'] if self.lod0 else off+4+len(current['name'])
                count=struct.unpack_from('>I',data,hdr+72)[0]
                source_count=(len(source['indices']) if self.lod0 else
                              struct.unpack_from('>I',self.original,source['offset']+4+len(source['name'])+72)[0])
                appended=count-source_count
                if appended<0 or appended%4:raise ValueError('Dev92: unexpected new triangle-strip layout')
                target_faces=[x for x in self.targets if x[0]==record_id and x[1]==donor]
                obj=target_faces[0][4]
                face_ids=obj.data.attributes['fh1_original_section_id']
                # The writer emits new faces in polygon order for each donor.
                all_faces=[]
                for poly in obj.data.polygons:
                    if face_ids.data[poly.index].value:continue
                    if self.lod0:
                        mat=obj.data.materials[poly.material_index]
                        custom=_shader(mat)
                        if custom and not any(sec['name'].lower()==custom.lower() for sec in oldsecs):
                            section_id=_donor(oldsecs)
                        else:
                            name=str(mat.get('fh1_shader_name','') or (mat.name[4:] if mat.name.startswith('FH1_') else mat.name))
                            section_id=next((i for i,s in enumerate(oldsecs) if s['name']==name),-1)
                    else:
                        mapping=list(obj.get('fh1_source_section_indices',[]))
                        section_id=mapping[poly.material_index] if poly.material_index<len(mapping) else -1
                    if section_id==donor:all_faces.append(poly.index)
                if len(all_faces)*4!=appended:
                    raise ValueError('Dev92: donor face count mismatch (%d vs %d); no output committed' % (len(all_faces)*4,appended))
                start=hdr+80+source_count*width;end=hdr+80+count*width
                values=struct.unpack_from('>'+str(appended)+('H' if width==2 else 'I'),data,start)
                groups={};remaining=[]
                by_face={x[2]:x[3] for x in target_faces}
                for k,face in enumerate(all_faces):
                    tri=list(values[4*k:4*k+4])
                    if len(tri)!=4 or tri[0]!=(65535 if width==2 else import_lod0.RESTART):
                        raise ValueError('Dev92: unrecognised appended strip')
                    shader=by_face.get(face)
                    if shader:groups.setdefault(shader,[]).extend(tri)
                    else:remaining.extend(tri)
                changes.append((start,end,struct.pack('>'+str(len(remaining))+('H' if width==2 else 'I'),*remaining)))
                changes.append((hdr+72,hdr+76,struct.pack('>I',source_count+len(remaining))))
                # Clone 77-byte native metadata prefix and original 80-byte section header.
                # The donor's atlas is intentionally shared for this first hierarchy test.
                prefix=data[off-77:off]
                header=bytearray(data[hdr:hdr+80])
                if not self.lod0 and struct.unpack_from('>I', header, 0)[0] != current['lod']:
                    raise ValueError('v5.0.3: donor LOD header mismatch')
                for shader,indices in groups.items():
                    struct.pack_into('>I',header,72,len(indices))
                    name=shader.encode('ascii')
                    additions.append(prefix+struct.pack('>I',len(name))+name+header+
                                     struct.pack('>'+str(len(indices))+('H' if width==2 else 'I'),*indices))
            if additions:
                # v5.0.3: Both native mesh families store a big-endian
                # section count at (first section name-length offset - 77).
                # Verified against all parsed components in the stock Corrado
                # and Mercedes SLS AMG exterior CARBINs. Fail closed if a
                # different record layout is encountered.
                first_name = (newsecs[0]['section_name_offset'] if self.lod0
                              else newsecs[0]['offset'])
                first = first_name - 77
                if first < 0 or first + 4 > len(data):
                    raise ValueError('v5.0.3: invalid native section count offset')
                declared = struct.unpack_from('>I', data, first)[0]
                if declared != len(newsecs):
                    raise ValueError(
                        'v5.0.3: section count mismatch on %s (header=%d, parsed=%d)' %
                        ((parsed[record_id]['label'] if self.lod0 else parsed[record_id]['name']),
                         declared, len(newsecs)))
                changes.append((first, first+4,
                                struct.pack('>I', declared + len(additions))))
                last = newsecs[-1]
                if self.lod0:
                    insert = last['end']
                else:
                    # LOD1-5 sections consist of: 77-byte prefix, 4-byte
                    # name length, name, 80-byte header, then index array.
                    # The parser's 'offset' points at the name length.
                    name_end = last['offset'] + 4 + len(last['name'])
                    last_count, last_width = struct.unpack_from('>II', data, name_end + 72)
                    if last_width not in (2, 4):
                        raise ValueError('v5.0.3: unsupported native index width')
                    insert = name_end + 80 + last_count * last_width
                    if insert > len(data) or insert > (parsed[record_id+1]['p'] if record_id+1 < len(parsed) else len(data)):
                        raise ValueError('v5.0.3: section insertion crosses component boundary')
                changes.append((insert, insert, b''.join(additions)))
        # Apply against the pre-edit byte offsets, from right to left.
        for start,end,replacement in sorted(changes,key=lambda x:(x[0],x[1]),reverse=True):
            data=data[:start]+replacement+data[end:]
        tmp=destination.with_name(destination.name+'.dev92-check')
        try:
            tmp.write_bytes(data)
            if self.lod0:
                result,_=import_lod0.parse(tmp)
            else:result=import_lod.discover(data)
            if len(result)!=len(orig):raise ValueError('Dev96: record count mismatch')
            # Compare post-insertion against the completed baseline export, not the original source.
            # Added geometry legitimately increases vertex counts before shader insertion.
            # Validate the entire component directory, not just the new shader
            # string. The previous experimental build passed while Forza Studio
            # displayed only a fraction of the vehicle.
            if self.lod0:
                before = [(b['label'], b['count'], [s['name'] for s in b['sections']]) for b in parsed]
                after = [(b['label'], b['count'], [s['name'] for s in b['sections']]) for b in result]
            else:
                before = [(r['name'], r['count'], [(s['name'],s['lod']) for s in r['sections']]) for r in parsed]
                after = [(r['name'], r['count'], [(s['name'],s['lod']) for s in r['sections']]) for r in result]
            for idx, (old, new) in enumerate(zip(before, after)):
                if old[:2] != new[:2]:
                    raise ValueError('Dev97: shader insertion changed baseline component identity or vertex count at %d (%s -> %s)' % (idx, old[:2], new[:2]))
                old_sections = old[2]
                new_sections = new[2]
                if new_sections[:len(old_sections)] != old_sections:
                    raise ValueError('Dev96: original shader hierarchy changed in component %s' % old[0])
                expected_new = {x[3] for x in self.targets if x[0] == idx}
                extra = new_sections[len(old_sections):]
                extra_names = {v if isinstance(v,str) else v[0] for v in extra}
                if extra_names != expected_new or len(extra) != len(expected_new):
                    raise ValueError('Dev96: unexpected shader sections in component %s: %s' % (old[0], extra))

            for rec_id in {x[0] for x in self.targets}:
                actual={s['name'] for s in result[rec_id]['sections']}
                expected={x[3] for x in self.targets if x[0]==rec_id}
                if not expected.issubset(actual):raise ValueError('Dev92: shader section missing after export')
            tmp.replace(destination)
        finally:
            if tmp.exists():tmp.unlink()
