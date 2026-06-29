#!/usr/bin/env python3
"""
objtex.py - Generate Tribes interior bitmaps (.bmp PBMP) for an OBJ's materials,
quantized to the interior texture palette, so a textured OBJ "just works" in the
engine without manual remapping to stock textures (WOOD4 etc.).

For each material used by <obj> (via its .mtl), emit <outdir>/<MatName>.bmp:
  * map_Kd present  -> load that image (PNG, or a Tribes/MS .bmp) and quantize.
  * no map_Kd       -> a solid swatch from the material's Kd diffuse color.

Tribes interior surface textures are PALETTIZED 8-bit: each pixel is an index
translated through a world multipalette chosen by the bitmap's paletteIndex
(engine/Dgfx/code/gOGLTx.cpp MPCacher). wood4.bmp & friends use paletteIndex
503 (the "interior structures" palette, identical across all world .ppl), so we
quantize to that 256-color table and stamp PiDX=503. Output is native PBMP with
a full mip chain (the release renderer reads pMipBits[mipLevel] without clamping
to detailLevels, so a single-level texture would read past the chain -> crash).

  python tools/objtex.py model.obj --outdir tex [--palette-index 503] [--ppl WORLD.vol:NAME.ppl] [--size 128]

Then pack with:  obj2vol.py ... --texdir tex
"""

import os
import sys
import struct
import zlib
import argparse

import volread
import textures


# --------------------------------------------------------------------------
# palette source (interior multipalette from a world .ppl)
# --------------------------------------------------------------------------

def _default_ppl():
    """No hardcoded path. If TRIBES_DIR is set, use its lush world palette (any
    world .ppl works — interior textures share paletteIndex 503). Else None."""
    td = os.environ.get("TRIBES_DIR")
    if td:
        return (os.path.join(td, "base", "lushWorld.vol"), "lush.day.ppl")
    return None


def load_palette(ppl_spec, index):
    """ppl_spec: 'vol.vol:name.ppl' or None. Returns [(r,g,b)]*256."""
    if ppl_spec:
        vp, _, nm = ppl_spec.partition(":")
    else:
        d = _default_ppl()
        if d is None:
            raise SystemExit(
                "objtex: no palette source. Pass --ppl "
                "\"<Tribes>/base/lushWorld.vol:lush.day.ppl\" (any world .ppl works), "
                "or set the TRIBES_DIR environment variable to your Tribes folder.")
        vp, nm = d
    tabs = textures.parse_ppl(volread.Vol(vp).read(nm))
    if index not in tabs:
        raise SystemExit("paletteIndex %s not in %s (have %s)" %
                         (index, nm, sorted(k for k in tabs if isinstance(k, int))))
    return tabs[index]


# --------------------------------------------------------------------------
# PNG reader (stdlib only; complements textures.write_png)
# --------------------------------------------------------------------------

def read_png(path):
    """Return (width, height, rgb_bytes top-down). Handles 8-bit grayscale/RGB/
    RGBA/palette PNGs (the common Blender/most-tools output)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s: not a PNG" % path)
    off = 8
    width = height = bitd = ctype = 0
    idat = bytearray()
    plte = None
    trns = None
    while off < len(data):
        ln = struct.unpack_from(">I", data, off)[0]
        tag = data[off + 4:off + 8]
        body = data[off + 8:off + 8 + ln]
        off += 12 + ln
        if tag == b"IHDR":
            width, height, bitd, ctype = struct.unpack_from(">IIBB", body, 0)[:4]
        elif tag == b"PLTE":
            plte = [(body[i], body[i + 1], body[i + 2]) for i in range(0, len(body), 3)]
        elif tag == b"tRNS":
            trns = body
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    if bitd != 8:
        raise ValueError("%s: only 8-bit PNGs supported (got bitdepth %d)" % (path, bitd))
    chans = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(bytes(idat))
    stride = width * chans
    # un-filter scanlines (PNG filter types 0-4)
    out = bytearray(stride * height)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        ft = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        if ft == 1:      # Sub
            for i in range(chans, stride):
                line[i] = (line[i] + line[i - chans]) & 0xff
        elif ft == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xff
        elif ft == 3:    # Average
            for i in range(stride):
                a = line[i - chans] if i >= chans else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xff
        elif ft == 4:    # Paeth
            for i in range(stride):
                a = line[i - chans] if i >= chans else 0
                b = prev[i]
                c = prev[i - chans] if i >= chans else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xff
        out[y * stride:(y + 1) * stride] = line
        prev = line
    # expand to RGB
    rgb = bytearray(width * height * 3)
    o = 0
    for i in range(width * height):
        s = i * chans
        if ctype == 2:       r, g, b = out[s], out[s + 1], out[s + 2]
        elif ctype == 6:     r, g, b = out[s], out[s + 1], out[s + 2]
        elif ctype == 0:     r = g = b = out[s]
        elif ctype == 4:     r = g = b = out[s]
        elif ctype == 3:     r, g, b = (plte or [(0, 0, 0)])[out[s]]
        rgb[o] = r; rgb[o + 1] = g; rgb[o + 2] = b; o += 3
    return width, height, bytes(rgb)


# --------------------------------------------------------------------------
# resize / mip / quantize
# --------------------------------------------------------------------------

def _pow2(n, cap):
    p = 1
    while p * 2 <= n and p * 2 <= cap:
        p *= 2
    return max(1, p)


def resize_nn(rgb, w, h, nw, nh):
    out = bytearray(nw * nh * 3)
    o = 0
    for y in range(nh):
        sy = y * h // nh
        for x in range(nw):
            sx = x * w // nw
            s = (sy * w + sx) * 3
            out[o] = rgb[s]; out[o + 1] = rgb[s + 1]; out[o + 2] = rgb[s + 2]; o += 3
    return bytes(out)


def downsample_half(rgb, w, h):
    """2x2 box filter -> (w//2, h//2). Assumes w,h even (>=2)."""
    nw, nh = w // 2, h // 2
    out = bytearray(nw * nh * 3)
    o = 0
    for y in range(nh):
        r0 = (2 * y) * w * 3
        r1 = (2 * y + 1) * w * 3
        for x in range(nw):
            a = r0 + 2 * x * 3
            b = r1 + 2 * x * 3
            for c in range(3):
                out[o + c] = (rgb[a + c] + rgb[a + 3 + c] + rgb[b + c] + rgb[b + 3 + c]) >> 2
            o += 3
    return bytes(out)


def quantize(rgb, w, h, palette, cache):
    """RGB -> 8-bit palette indices (nearest color, cached)."""
    out = bytearray(w * h)
    for i in range(w * h):
        s = i * 3
        key = (rgb[s], rgb[s + 1], rgb[s + 2])
        idx = cache.get(key)
        if idx is None:
            r, g, b = key
            best = 0; bestd = 1 << 30
            for pi, (pr, pg, pb) in enumerate(palette):
                d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
                if d < bestd:
                    bestd = d; best = pi
                    if d == 0:
                        break
            cache[key] = idx = best
        out[i] = idx
    return bytes(out)


# --------------------------------------------------------------------------
# PBMP writer (mirrors engine/Dgfx/code/g_bitmap.cpp GFXBitmap::write)
# --------------------------------------------------------------------------

def write_pbmp(path, w, h, palette, palette_index):
    """Quantize an RGB image (already pow2) to the palette, build the full mip
    chain, and write a native PBMP. `palette` is [(r,g,b)]*256; the caller passes
    pre-resized RGB via the module-level _rgb attr is avoided -- we take rgb arg
    through write_pbmp_rgb instead. (kept for clarity)"""
    raise NotImplementedError  # use write_pbmp_rgb


def write_pbmp_rgb(path, rgb, w, h, palette, palette_index):
    cache = {}
    # mip chain: level0 = w*h, halving until min dim hits 1
    levels = []
    cw, ch, crgb = w, h, rgb
    while True:
        levels.append(quantize(crgb, cw, ch, palette, cache))
        if cw == 1 or ch == 1:
            break
        crgb = downsample_half(crgb, cw, ch)
        cw, ch = cw // 2, ch // 2
    detail_levels = len(levels)
    bits = b"".join(levels)
    image_size = len(bits)

    BMP = struct.unpack("<I", b"PBMP")[0]
    HEAD = struct.unpack("<I", b"head")[0]
    DATA = struct.unpack("<I", b"data")[0]
    DETL = struct.unpack("<I", b"DETL")[0]
    PIDX = struct.unpack("<I", b"PiDX")[0]

    head_body = struct.pack("<IIIII",
                            0 + 3,          # ver_nc: BITMAP_VERSION(0) + 3 chunks
                            w, h, 8, 0)     # width, height, bitDepth, attribute
    body = b""
    body += struct.pack("<II", HEAD, len(head_body)) + head_body
    body += struct.pack("<II", DATA, image_size) + bits
    body += struct.pack("<II", DETL, 4) + struct.pack("<i", detail_levels)
    body += struct.pack("<II", PIDX, 4) + struct.pack("<I", palette_index & 0xffffffff)

    out = struct.pack("<II", BMP, len(body)) + body
    with open(path, "wb") as f:
        f.write(out)
    return detail_levels, w, h


# --------------------------------------------------------------------------
# MTL parsing
# --------------------------------------------------------------------------

def parse_mtl(path):
    """Return {name: {'map': path|None, 'kd': (r,g,b)}}."""
    mats = {}
    cur = None
    base = os.path.dirname(path)
    if not os.path.isfile(path):
        return mats
    for line in open(path):
        t = line.split()
        if not t:
            continue
        if t[0] == "newmtl":
            cur = t[1]; mats[cur] = {"map": None, "kd": (200, 200, 200)}
        elif cur is None:
            continue
        elif t[0] == "Kd":
            mats[cur]["kd"] = tuple(int(max(0.0, min(1.0, float(v))) * 255 + 0.5) for v in t[1:4])
        elif t[0] == "map_Kd":
            f = line.split(None, 1)[1].strip().strip('"')
            mats[cur]["map"] = f if os.path.isabs(f) else os.path.join(base, f)
    return mats


def obj_materials_and_mtllib(obj_path):
    """Return (ordered material names used, mtllib path or None)."""
    used = []
    mtllib = None
    base = os.path.dirname(obj_path)
    for line in open(obj_path):
        t = line.split()
        if not t:
            continue
        if t[0] == "mtllib":
            f = line.split(None, 1)[1].strip()
            mtllib = f if os.path.isabs(f) else os.path.join(base, f)
        elif t[0] == "usemtl":
            n = t[1]
            if n not in used:
                used.append(n)
    return used, mtllib


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate Tribes interior .bmp textures for an OBJ's materials.")
    ap.add_argument("obj")
    ap.add_argument("--outdir", required=True, help="directory to write <Material>.bmp into")
    ap.add_argument("--palette-index", type=int, default=503, help="interior paletteIndex (default 503)")
    ap.add_argument("--ppl", default=None, help="palette source 'world.vol:name.ppl' "
                    "(else uses $TRIBES_DIR/base/lushWorld.vol; required if neither is set)")
    ap.add_argument("--size", type=int, default=128, help="max texture dimension (pow2-capped, default 128)")
    ap.add_argument("--solid-size", type=int, default=16, help="dimension for color-only swatches (default 16)")
    args = ap.parse_args()

    palette = load_palette(args.ppl, args.palette_index)
    os.makedirs(args.outdir, exist_ok=True)

    used, mtllib = obj_materials_and_mtllib(args.obj)
    mats = parse_mtl(mtllib) if mtllib else {}
    if not used:
        used = list(mats.keys())
    print("materials: %s  (paletteIndex %d)" % (used, args.palette_index))

    for name in used:
        m = mats.get(name, {"map": None, "kd": (200, 200, 200)})
        outp = os.path.join(args.outdir, name + ".bmp")
        if m["map"] and os.path.isfile(m["map"]):
            w0, h0, rgb = read_png(m["map"]) if m["map"].lower().endswith(".png") \
                else _read_any_bmp(m["map"])
            nw, nh = _pow2(w0, args.size), _pow2(h0, args.size)
            if (nw, nh) != (w0, h0):
                rgb = resize_nn(rgb, w0, h0, nw, nh)
            dl, fw, fh = write_pbmp_rgb(outp, rgb, nw, nh, palette, args.palette_index)
            print("  %-12s <- %s  %dx%d  mips=%d" % (name + ".bmp", os.path.basename(m["map"]), fw, fh, dl))
        else:
            sz = _pow2(args.solid_size, args.solid_size)
            r, g, b = m["kd"]
            rgb = bytes((r, g, b)) * (sz * sz)
            dl, fw, fh = write_pbmp_rgb(outp, rgb, sz, sz, palette, args.palette_index)
            # report the actual quantized color so we know what it'll look like
            qi = quantize(bytes((r, g, b)), 1, 1, palette, {})[0]
            print("  %-12s <- Kd(%d,%d,%d) -> palette[%d]=%s  %dx%d" %
                  (name + ".bmp", r, g, b, qi, palette[qi], fw, fh))


def _read_any_bmp(path):
    """Read a Tribes/MS .bmp via textures.parse_bitmap + its own palette (best
    effort for image maps that are already .bmp)."""
    with open(path, "rb") as f:
        data = f.read()
    bmp = textures.parse_bitmap(data)
    pal = bmp["embedded_palette"] or [(i, i, i) for i in range(256)]
    w, h, rgb = textures.expand_rgb(bmp, pal)
    return w, h, rgb


if __name__ == "__main__":
    main()
