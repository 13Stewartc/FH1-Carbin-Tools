"""Experimental native FH1 XDS BC1/BC3/BC5 loader (Blender 3.6, no DLL).

Preserves CARBIN mesh importers. Images are decoded from the top mip only.
The Xbox colour curve is empirically matched to Forza Studio, not guaranteed
for every shader. BC5 stores XY channels; no invented Z channel is written.
"""
import os
import struct
import math

FORMATS = {0x73: ('BC1', 8), 0x75: ('BC3', 16), 0x71: ('BC5', 16)}


def inspect_xds(path):
    with open(path, 'rb') as stream:
        h = stream.read(52)
    if len(h) != 52 or struct.unpack_from('>II', h) != (3, 1):
        raise ValueError('Unrecognised FH1 XDS header: %s' % path)
    code = h[35]
    if code not in FORMATS:
        raise ValueError('Unsupported XDS format 0x%02X' % code)
    dim = int.from_bytes(h[36:40], 'big')
    width = (dim & 0x1fff) + 1
    height = ((dim >> 13) & 0x1fff) + 1
    if not (1 <= width <= 8192 and 1 <= height <= 8192):
        raise ValueError('Invalid XDS dimensions')
    return {'path': path, 'width': width, 'height': height,
            'format': FORMATS[code][0], 'code': code, 'header_size': 52}


def tiled_byte_offset(x, y, aligned_block_width, log2_block_bytes):
    micro = ((x & 7) + ((y & 6) * 4)) << log2_block_bytes
    base = ((micro & 15) + ((y >> 3 & 1) << (log2_block_bytes + 6))
            + (((x >> 5) + (y >> 5) * (aligned_block_width >> 5))
               << (log2_block_bytes + 6)) * 2
            + (micro & ~15) * 2 + (y & 1) * 16)
    lane = (y >> 4 & 1) + (((x >> 3) + ((y >> 3 & 1) * 2)) & 3) * 2
    segment = (lane & ~1) + (((base >> 6 & 7) + (lane & 1) * 8) * 8)
    return ((base & ~511) + segment * 4) * 8 + (base & 63)


def _bc_alpha(block):
    a, b = block[0], block[1]
    if a > b:
        palette = [a, b] + [((7-i)*a + i*b)//7 for i in range(1, 7)]
    else:
        palette = [a, b] + [((5-i)*a + i*b)//5 for i in range(1, 5)] + [0, 255]
    bits = int.from_bytes(block[2:8], 'little')
    return [palette[(bits >> (3*i)) & 7] for i in range(16)]


def _rgb565(c):
    return (((c >> 11) & 31)*255//31, ((c >> 5) & 63)*255//63, (c & 31)*255//31)


def _bc_colour(block, bc1):
    c0, c1, bits = struct.unpack('<HHI', block)
    p0, p1 = _rgb565(c0), _rgb565(c1)
    if bc1 and c0 <= c1:
        palette = [p0, p1, tuple((p0[k]+p1[k])//2 for k in range(3)), (0, 0, 0)]
        alphas = [255, 255, 255, 0]
    else:
        palette = [p0, p1,
                   tuple((2*p0[k]+p1[k])//3 for k in range(3)),
                   tuple((p0[k]+2*p1[k])//3 for k in range(3))]
        alphas = [255]*4
    return [(palette[(bits >> (2*i)) & 3], alphas[(bits >> (2*i)) & 3]) for i in range(16)]


def _xbox_rgb(v):
    # Forza Studio's observed transfer curve; retained as experimental.
    if v < 64: return round(v/4)
    if v < 128: return round((v-64)/2+16)
    if v < 192: return v-64
    if v == 255: return 255
    return min(255, (v-192)*2+128)


def decode_xds_bytes(raw, *, colour_correction=True):
    """Return (width, height, RGBA bytes, metadata). Pixel rows are top-down."""
    if len(raw) < 52 or raw[:8] != b'\0\0\0\3\0\0\0\1':
        raise ValueError('Invalid XDS v3 header')
    code = raw[35]
    if code not in FORMATS: raise ValueError('Unsupported XDS format 0x%02X' % code)
    fmt, block_size = FORMATS[code]
    dim = int.from_bytes(raw[36:40], 'big')
    w, h = (dim & 0x1fff)+1, ((dim >> 13) & 0x1fff)+1
    if not (1 <= w <= 8192 and 1 <= h <= 8192): raise ValueError('Invalid dimensions')
    bw, bh = (w+3)//4, (h+3)//4
    aw, ah = (bw+31)&~31, (bh+31)&~31
    payload = memoryview(raw)[52:]
    if len(payload) < aw*ah*block_size:
        raise ValueError('Truncated XDS base mip')
    pixels = bytearray(w*h*4)
    shift = 3 if block_size == 8 else 4
    for by in range(bh):
        for bx in range(bw):
            off = tiled_byte_offset(bx, by, aw, shift)
            if off+block_size > len(payload): raise ValueError('Invalid tiled block offset')
            src = payload[off:off+block_size]
            block = bytearray(block_size)
            block[0::2], block[1::2] = src[1::2], src[0::2]
            if fmt == 'BC5':
                r, g = _bc_alpha(block[:8]), _bc_alpha(block[8:])
                # Preserve raw Forza Studio-style BC5 output; no normal reconstruction.
                texels = [(r[i], g[i], 0, 255) for i in range(16)]
            else:
                colours = _bc_colour(block[0:8] if fmt == 'BC1' else block[8:16], fmt == 'BC1')
                alphas = [255]*16 if fmt == 'BC1' else _bc_alpha(block[:8])
                texels = [(*colours[i][0], min(alphas[i], colours[i][1])) for i in range(16)]
            for iy in range(4):
                y = by*4+iy
                if y >= h: continue
                for ix in range(4):
                    x = bx*4+ix
                    if x >= w: continue
                    r,g,b,a = texels[iy*4+ix]
                    if colour_correction and fmt != 'BC5':
                        r,g,b = _xbox_rgb(r),_xbox_rgb(g),_xbox_rgb(b)
                    dst = (y*w+x)*4
                    pixels[dst:dst+4] = bytes((r,g,b,a))
    return w,h,bytes(pixels),{'format':fmt,'code':code}


def _bc5_to_opengl_rgba(rgba):
    """Convert decoded BC5 XY to OpenGL tangent-space RGB (Y up).

    Preserve raw BC5 RGBA for decoding diagnostics; convert a separate copy.
    The source stores X in R and Y in G using the DirectX green convention.
    """
    import math
    result = bytearray(len(rgba))
    for i in range(0, len(rgba), 4):
        r, g = rgba[i], rgba[i + 1]
        x = r / 127.5 - 1.0
        y = 1.0 - g / 127.5  # DirectX -> OpenGL (invert green)
        z = math.sqrt(max(0.0, 1.0 - x*x - y*y))
        result[i] = r
        result[i + 1] = 255 - g
        result[i + 2] = min(255, max(0, round((z + 1.0) * 127.5)))
        result[i + 3] = 255
    return bytes(result)


def discover_xds(carbin_path):
    folder = os.path.dirname(os.path.abspath(carbin_path))
    return [os.path.join(root, name) for root, dirs, files in os.walk(folder)
            for name in files if name.lower().endswith('.xds')]


def _write_rgba_png(path, width, height, rgba):
    """Write unmodified top-down RGBA bytes as PNG using only Python stdlib."""
    import struct
    import zlib
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data +
                struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))
    stride = width * 4
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # PNG filter: none
        rows.extend(rgba[y * stride:(y + 1) * stride])
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')
        f.write(chunk(b'IHDR', ihdr))
        f.write(chunk(b'IDAT', zlib.compress(rows, 3)))
        f.write(chunk(b'IEND', b''))


def load_xds_image(path, name=None):
    """Decode XDS to a real PNG for every format, then load via Blender image IO."""
    import bpy
    import zlib
    import time
    import tempfile
    path = os.path.normcase(os.path.abspath(path))
    stat = os.stat(path)
    identity = '%s|%s|%s|openglbc5_v4' % (path, stat.st_size, stat.st_mtime_ns)
    token = '%08x' % (zlib.crc32(identity.encode('utf-8')) & 0xffffffff)
    name = name or ('FH1 XDS GL | ' + os.path.basename(path) + ' | ' + token)
    image = bpy.data.images.get(name)
    if (image is not None and image.get('fh1_xds_identity') == identity
            and image.size[0] > 0 and image.size[1] > 0):
        return image, False
    started = time.perf_counter()
    with open(path, 'rb') as stream:
        w, h, rgba, meta = decode_xds_bytes(stream.read())
    # Use the known-good disk PNG path for ALL formats. Direct foreach_set()
    # uploads proved unreliable in Blender 3.6 for some XDS images.
    output_dir = os.path.join(tempfile.gettempdir(), 'FH1_XDS_Raw_Diagnostics')
    os.makedirs(output_dir, exist_ok=True)
    if meta['format'] == 'BC5':
        raw_path = os.path.join(output_dir, token + '_raw_' + os.path.basename(path) + '.png')
        png_path = os.path.join(output_dir, token + '_opengl_' + os.path.basename(path) + '.png')
        if not os.path.isfile(raw_path):
            _write_rgba_png(raw_path, w, h, rgba)
        if not os.path.isfile(png_path):
            _write_rgba_png(png_path, w, h, _bc5_to_opengl_rgba(rgba))
    else:
        png_path = os.path.join(output_dir, token + '_decoded_' + os.path.basename(path) + '.png')
        if not os.path.isfile(png_path):
            _write_rgba_png(png_path, w, h, rgba)
    if image is not None:
        bpy.data.images.remove(image)
    image = bpy.data.images.load(png_path, check_existing=False)
    image.name = name
    image.colorspace_settings.name = 'Non-Color' if meta['format'] == 'BC5' else 'sRGB'
    image.alpha_mode = 'STRAIGHT'
    image.reload()
    print('[FH1 XDS] Disk PNG: ' + png_path)
    image['fh1_xds_format'] = meta['format']
    image['fh1_xds_decoder_revision'] = 5
    image['fh1_xds_identity'] = identity
    image['fh1_xds_source'] = path
    image['fh1_xds_size'] = stat.st_size
    image['fh1_xds_mtime_ns'] = str(stat.st_mtime_ns)
    print('[FH1 XDS] %s %dx%d decoded in %.2fs' %
          (os.path.basename(path), w, h, time.perf_counter()-started))
    return image, True


def load_xds_images(carbin_path):
    """Explicit bulk-load mode, used only when material resolution is disabled."""
    loaded, failures = {}, []
    for path in discover_xds(carbin_path):
        try:
            stem = os.path.relpath(path, os.path.dirname(os.path.abspath(carbin_path))).replace('\\','/')
            image, _ = load_xds_image(path)
            loaded[stem] = image
        except Exception as exc:
            failures.append((path, str(exc)))
            print('[FH1 XDS] Failed %s: %s' % (path, exc))
    return loaded, failures

def report_xds(carbin_path):
    return [inspect_xds(p) for p in discover_xds(carbin_path)]
