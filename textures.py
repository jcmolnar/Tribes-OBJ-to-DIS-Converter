#!/usr/bin/env python3
"""
textures.py - Decode Starsiege Tribes / Darkstar palettized bitmaps to PNG.

Two on-disk bitmap forms (engine/Dgfx/code/g_bitmap.cpp GFXBitmap::read):
  * PBMP  : chunked - 'PBMP','head'(BMPHeader),'data'(8-bit indices+mipmaps),
            'DETL'(detailLevels),'PiDX'(paletteIndex). No embedded palette ->
            colors come from an external world palette (.ppl) chosen by paletteIndex.
  * MS DIB: a standard Windows BMP ('BM') with its own palette embedded.

World palette (.ppl) "PL98" format (engine/Dgfx/code/g_pal.cpp GFXPalette::read):
  FOURCC 'PL98'; int numPalettes, shadeShift, hazeLevels, hazeColor;
  32 bytes allowedColorMatches; then per multipalette:
     1024 bytes color[256] (PALETTEENTRY rgbx), int paletteIndex, int paletteType.
A texture's paletteIndex selects the matching multipalette's 256-color table.

PNG is written with only the stdlib (zlib), so no Pillow dependency.
"""

import struct
import zlib


# --------------------------------------------------------------------------
# PL98 world palette
# --------------------------------------------------------------------------

def parse_ppl(data):
    """Return {paletteIndex: [(r,g,b)]*256}. Also keyed by None for palette[0]."""
    off = 0
    typ = data[off:off + 4]; off += 4
    if typ != b"PL98":
        raise ValueError(f"not a PL98 palette (got {typ!r})")
    numPal, shadeShift, hazeLevels, hazeColor = struct.unpack_from("<iiii", data, off)
    off += 16
    off += 32  # allowedColorMatches BitVector (256 bits)

    tables = {}
    first = None
    for i in range(numPal):
        colors = []
        for c in range(256):
            r, g, b, _flags = data[off + c * 4: off + c * 4 + 4]
            colors.append((r, g, b))
        off += 1024
        pidx, ptype = struct.unpack_from("<ii", data, off); off += 8
        tables[pidx] = colors
        if first is None:
            first = colors
    tables[None] = first  # fallback
    return tables


# --------------------------------------------------------------------------
# Tribes bitmaps
# --------------------------------------------------------------------------

def _fourcc(s):
    return struct.unpack("<I", s.encode("ascii"))[0]


def parse_bitmap(data):
    """Parse a GFXBitmap. Returns dict: width,height,indices(level0 bytes),
    paletteIndex, embedded_palette (list[(r,g,b)] or None)."""
    # MS DIB? first 2 bytes 'BM'
    if data[:2] == b"BM":
        return _parse_msdib(data)
    return _parse_pbmp(data)


def _parse_pbmp(data):
    off = 0
    width = height = bitDepth = 0
    stride = 0
    indices = None
    paletteIndex = None
    embedded = None
    num_chunks = -1
    HEAD = _fourcc("head"); DATA = _fourcc("data"); DETL = _fourcc("DETL")
    PIDX = _fourcc("PiDX"); RIFF = _fourcc("RIFF"); PBMP = _fourcc("PBMP")

    while num_chunks:
        num_chunks -= 1
        if off + 8 > len(data):
            break
        cid, csize = struct.unpack_from("<II", data, off); off += 8
        if cid == PBMP:
            continue
        elif cid == HEAD:
            ver_nc, width, height, bitDepth, attribute = struct.unpack_from(
                "<IIIII", data, off)
            num_chunks = ver_nc & 0x00ffffff
            stride = ((width * bitDepth >> 3) + 3) & ~3
            off += csize
        elif cid == DATA:
            indices = data[off:off + csize]
            off += csize
        elif cid == DETL:
            off += csize
        elif cid == PIDX:
            paletteIndex = struct.unpack_from("<I", data, off)[0]
            off += csize
        elif cid == RIFF:
            off += csize  # embedded MS palette - rarely present; skip for now
        else:
            off += csize

    if indices is None or bitDepth != 8:
        raise ValueError(f"unsupported PBMP (bitDepth={bitDepth})")
    # level 0 image is the first height*stride bytes (rest are mipmaps)
    level0 = indices[:height * stride]
    return {
        "width": width, "height": height, "stride": stride,
        "indices": level0, "paletteIndex": paletteIndex,
        "embedded_palette": embedded,
    }


def _parse_msdib(data):
    # BITMAPFILEHEADER (14) + BITMAPINFOHEADER (40)
    (bfType, bfSize, bfRes1, bfRes2, bfOffBits) = struct.unpack_from("<HIHHI", data, 0)
    (biSize, biWidth, biHeight, biPlanes, biBitCount, biCompression,
     biSizeImage, biXPM, biYPM, biClrUsed, biClrImportant) = struct.unpack_from(
        "<IiiHHIIiiII", data, 14)
    if biBitCount != 8:
        raise ValueError(f"MS DIB bitDepth {biBitCount} unsupported")
    width = biWidth
    height = abs(biHeight)
    stride = ((width * biBitCount >> 3) + 3) & ~3
    ncolors = biClrUsed if biClrUsed else 256
    pal_off = 14 + biSize
    embedded = []
    for c in range(ncolors):
        b, g, r, _ = data[pal_off + c * 4: pal_off + c * 4 + 4]  # RGBQUAD = BGRx
        embedded.append((r, g, b))
    while len(embedded) < 256:
        embedded.append((0, 0, 0))
    bits = data[bfOffBits:bfOffBits + height * stride]
    rows = [bits[y * stride:(y + 1) * stride] for y in range(height)]
    if biHeight > 0:           # bottom-up DIB -> flip to top-down
        rows.reverse()
    level0 = b"".join(rows)
    paletteIndex = bfRes2 if bfRes1 == 0xf5f7 and bfRes2 != 0xffff else None
    return {
        "width": width, "height": height, "stride": stride,
        "indices": level0, "paletteIndex": paletteIndex,
        "embedded_palette": embedded,
    }


# --------------------------------------------------------------------------
# RGB expansion + PNG output
# --------------------------------------------------------------------------

def expand_rgb(bmp, palette):
    """palette: list[(r,g,b)]*256. Returns (width, height, bytes RGB top-down)."""
    w, h, stride = bmp["width"], bmp["height"], bmp["stride"]
    idx = bmp["indices"]
    out = bytearray(w * h * 3)
    o = 0
    for y in range(h):
        row = y * stride
        for x in range(w):
            r, g, b = palette[idx[row + x]]
            out[o] = r; out[o + 1] = g; out[o + 2] = b; o += 3
    return w, h, bytes(out)


def write_png(path, width, height, rgb):
    """Minimal RGB PNG via stdlib zlib."""
    def chunk(tag, body):
        c = tag + body
        return struct.pack(">I", len(body)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    # add the per-row filter byte (0 = none)
    raw = bytearray()
    rowlen = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * rowlen:(y + 1) * rowlen]

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit, RGB
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))
