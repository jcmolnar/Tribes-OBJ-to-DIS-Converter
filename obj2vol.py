#!/usr/bin/env python3
"""
obj2vol.py - PoC: build a Tribes interior .vol (.dis + .dig + .dml) from an OBJ.

This is the inverse of dis2obj.py. It writes the real on-disk formats:
  .dig  ITRGeometry  (PERS block) - geometry arrays
  .dml  TS::MaterialList (PERS block) - texture names
  .dis  ITRShape (ITRs tag block)  - the manifest naming the .dig
  .vol  PVOL archive packing all three

SCOPE / CAVEAT: the .dig's BSP (nodeList / leaves / PVS) is written EMPTY. That
is enough to (a) serialize a structurally valid ITRGeometry and (b) round-trip
through dis2obj.py, which is what this PoC proves. It is NOT enough for the live
engine to render/cull - that needs a real BSP from ITRBSPBuild::buildTree (the
engine's compiler). This proves the easy 80% (write + pack); the BSP compile is
the documented next step.

Usage:
    python obj2vol.py model.obj [-o out.vol] [--name shapename]
"""

import sys
import os
import struct
import argparse


# ---------------------------------------------------------------- OBJ parsing
def parse_obj(path):
    verts = []      # (x,y,z)
    uvs = []        # (u,v)
    faces = []      # (material_index, [(vidx, vtidx), ...])  0-based
    mats = []       # material names in first-use order
    mat_idx = {}
    cur_mat = 0
    for line in open(path):
        t = line.split()
        if not t:
            continue
        if t[0] == "v":
            verts.append((float(t[1]), float(t[2]), float(t[3])))
        elif t[0] == "vt":
            uvs.append((float(t[1]), float(t[2])))
        elif t[0] == "usemtl":
            name = t[1]
            if name not in mat_idx:
                mat_idx[name] = len(mats)
                mats.append(name)
            cur_mat = mat_idx[name]
        elif t[0] == "f":
            corners = []
            for tok in t[1:]:
                parts = tok.split("/")
                vi = int(parts[0]) - 1
                vti = (int(parts[1]) - 1) if len(parts) > 1 and parts[1] else -1
                corners.append((vi, vti))
            faces.append((cur_mat, corners))
    if not mats:
        mats = ["default"]
    return verts, uvs, faces, mats


# ------------------------------------------------------------- PERS framing
def pers_block(classname, version, body):
    """Wrap body as a 'PERS' + name + version block (IMPLEMENT_PERSISTENT).
    The name field on disk is exactly (namesize+1)&~1 bytes (the reader reads
    that many and null-terminates itself), so an even-length name stores NO
    trailing null."""
    name = classname.encode("ascii")
    fieldlen = (len(name) + 1) & ~1
    namefield = name + b"\x00" * (fieldlen - len(name))
    payload = struct.pack("<H", len(name)) + namefield + struct.pack("<i", version) + body
    return b"PERS" + struct.pack("<I", len(payload)) + payload


def tag_block(fourcc, version, body):
    """Wrap body as a tagged block (IMPLEMENT_PERSISTENT_TAG), e.g. 'ITRs'."""
    payload = struct.pack("<i", version) + body
    return fourcc + struct.pack("<I", len(payload)) + payload


# ------------------------------------------------------------- .dig writer
def build_dig(verts, uvs, faces):
    # point3List / point2List straight from the OBJ
    point3 = verts
    point2 = uvs if uvs else [(0.0, 0.0)]

    # vertexList + surfaceList + planeList, one surface per face
    vertexList = []     # (pointIndex, textureIndex)
    surfaces = []       # raw 20-byte records
    planes = []         # (a,b,c,d)

    def face_plane(corners):
        # Newell normal + plane distance through the first vertex
        nx = ny = nz = 0.0
        n = len(corners)
        for i in range(n):
            a = verts[corners[i][0]]; b = verts[corners[(i + 1) % n][0]]
            nx += (a[1] - b[1]) * (a[2] + b[2])
            ny += (a[2] - b[2]) * (a[0] + b[0])
            nz += (a[0] - b[0]) * (a[1] + b[1])
        L = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
        nx, ny, nz = nx / L, ny / L, nz / L
        p0 = verts[corners[0][0]]
        d = -(nx * p0[0] + ny * p0[1] + nz * p0[2])
        return (nx, ny, nz, d)

    for mat, corners in faces:
        vidx = len(vertexList)
        for (vi, vti) in corners:
            vertexList.append((vi, vti if vti >= 0 else 0))
        plane_index = len(planes)
        planes.append(face_plane(corners))

        # Surface (20 bytes). type=Material(0), applyAmbient+visibleToOutside+
        # planeFront set; full-texture mapping (offset 0, size 255 -> +1=256).
        bits = 0x20 | 0x40 | 0x80            # applyAmbient | visibleToOutside | planeFront
        rec = bytearray(20)
        rec[0] = bits
        rec[1] = mat & 0xFF                  # material
        rec[2] = 255                         # textureSize.x (stored +1 -> 256)
        rec[3] = 255                         # textureSize.y
        rec[4] = 0                           # textureOffset.x
        rec[5] = 0                           # textureOffset.y
        struct.pack_into("<H", rec, 6, plane_index & 0xFFFF)
        struct.pack_into("<I", rec, 8, vidx)            # vertexIndex
        struct.pack_into("<I", rec, 12, 0)              # pointIndex (bitList) - unused
        rec[16] = len(corners) & 0xFF                   # vertexCount
        rec[17] = 0                                     # pointCount
        surfaces.append(bytes(rec))

    # bounding box
    xs = [p[0] for p in point3]; ys = [p[1] for p in point3]; zs = [p[2] for p in point3]
    box = (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    # --- assemble ITRGeometry::read byte layout ---
    out = bytearray()
    out += struct.pack("<i", 1)                 # buildId
    out += struct.pack("<f", 16.0)              # textureScale
    out += struct.pack("<6f", *box)             # box (Box3F)

    # 9 sizes: surface, node, solidleaf, emptyleaf, bit, vertex, point3, point2, plane
    out += struct.pack("<i", len(surfaces))
    out += struct.pack("<i", 0)                 # nodeList (empty BSP - PoC)
    out += struct.pack("<i", 0)                 # solidLeafList
    out += struct.pack("<i", 0)                 # emptyLeafList
    out += struct.pack("<i", 0)                 # bitList
    out += struct.pack("<i", len(vertexList))
    out += struct.pack("<i", len(point3))
    out += struct.pack("<i", len(point2))
    out += struct.pack("<i", len(planes))

    # arrays (same order)
    for s in surfaces:
        out += s
    # node/solidleaf/emptyleaf/bit are empty
    for (pi, ti) in vertexList:
        out += struct.pack("<HH", pi & 0xFFFF, ti & 0xFFFF)
    for (x, y, z) in point3:
        out += struct.pack("<fff", x, y, z)
    for (u, v) in point2:
        out += struct.pack("<ff", u, v)
    for (a, b, c, d) in planes:
        out += struct.pack("<ffff", a, b, c, d)

    out += struct.pack("<i", 0)                 # highestMipLevel
    out += struct.pack("<I", 0)                 # flags

    return pers_block("ITRGeometry", 7, bytes(out))


# ------------------------------------------------------------- .dml writer
def build_dml(mats):
    body = bytearray()
    body += struct.pack("<i", 1)                # fnDetails
    body += struct.pack("<i", len(mats))        # fnMaterials
    for name in mats:
        fname = name if name.lower().endswith(".bmp") else name + ".bmp"
        rec = bytearray(64)
        struct.pack_into("<i", rec, 0, 0x03)    # fFlags = MatTexture
        struct.pack_into("<f", rec, 4, 1.0)     # fAlpha
        struct.pack_into("<i", rec, 8, 0)       # fIndex
        struct.pack_into("<I", rec, 12, 0)      # fRGB
        enc = fname.encode("latin1")[:31]
        rec[16:16 + len(enc)] = enc             # fMapFile[32]
        struct.pack_into("<i", rec, 48, 0)      # fType
        struct.pack_into("<f", rec, 52, 1.0)    # fElasticity
        struct.pack_into("<f", rec, 56, 1.0)    # fFriction
        struct.pack_into("<I", rec, 60, 1)      # fUseDefaultProps
        body += rec
    return pers_block("TS::MaterialList", 4, bytes(body))


# ------------------------------------------------------------- .dis writer
def build_dis(shape_name, dig_name, dml_name, dil_name=None):
    # string table order matches the stock interiors: state, geom, [dil], dml, lightstate
    names = b""
    state_off = len(names); names += b"State0\x00"
    geom_off = len(names);  names += dig_name.encode("latin1") + b"\x00"
    if dil_name:
        dil_off = len(names); names += dil_name.encode("latin1") + b"\x00"
    mat_off = len(names);   names += dml_name.encode("latin1") + b"\x00"
    light_state_name_off = len(names); names += b"default\x00"

    body = bytearray()
    # stateVector: 1 state {nameIndex, lodIndex, numLODs}
    body += struct.pack("<I", 1)
    body += struct.pack("<III", state_off, 0, 1)
    # lodVector: 1 lod {minPixels, geometryFileOffset, lightStateIndex, linkableFaces}
    body += struct.pack("<I", 1)
    body += struct.pack("<IIII", 250, geom_off, 0, 0xFF)
    # lodLightStates: 1 -> the .dil (a LOD with lightStateIndex but 0 states crashes
    # the engine on load). If no .dil, write 0 (round-trip only, not engine-loadable).
    if dil_name:
        body += struct.pack("<I", 1)
        body += struct.pack("<I", dil_off)
    else:
        body += struct.pack("<I", 0)
    # numLightStates + names
    body += struct.pack("<I", 1)
    body += struct.pack("<I", light_state_name_off)
    # nameBuffer
    body += struct.pack("<I", len(names))
    body += names
    # materialListOffset
    body += struct.pack("<I", mat_off)
    # m_linkedInterior is a bool (1 byte) -- writing 4 here makes the .dis fail to load
    body += struct.pack("<B", 0)
    return tag_block(b"ITRs", 3, bytes(body))


# ------------------------------------------------------------- .vol writer
def _name_id(name):
    # simple stable 32-bit hash for the VolumeItem.ID (engine usually looks up
    # by string; ID just needs to be consistent for its own sort)
    h = 0
    for ch in name.lower():
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def build_vol(entries):
    """entries: list of (name, data_bytes). Returns the .vol bytes (PVOL).
    Every block starts on a 4-byte boundary, matching the engine's BlockRWStream
    (which aligns block reads); without this the engine misreads the directory."""
    out = bytearray()

    def align4():
        while len(out) % 4:
            out.append(0)

    out += b"PVOL"
    out += struct.pack("<I", 0)          # stringBlockOffset placeholder

    items = []   # (name, block_offset, size)
    for name, data in entries:
        align4()
        block_off = len(out)
        out += b"VBLK" + struct.pack("<I", len(data)) + data
        items.append((name, block_off, len(data)))

    align4()
    string_block_off = len(out)

    # 'vols' block: concatenated null-terminated names; record each offset
    strtab = bytearray()
    str_offsets = []
    for (name, _, _) in items:
        str_offsets.append(len(strtab))
        strtab += name.encode("latin1") + b"\x00"
    out += b"vols" + struct.pack("<I", len(strtab)) + strtab

    # 'voli' block: VolumeItem[] (17 bytes packed).
    # The engine finds voli at vols_start + 8 + alignSize(strtab_len, WORD) --
    # BlockStream rounds a non-ALIGN_DWORD block's data size UP TO 2 bytes, NOT 4
    # (blkstrm.h getBlockSize/alignSize). A 4-byte align here over-pads whenever
    # strtab_len % 4 != 0, shifting voli past where the engine reads it -> the
    # directory misreads -> 0 files indexed -> "Could not load interior". Pad to
    # WORD to match the engine exactly. (vols_start+8 is already even, so this
    # rounds strtab to an even length, == alignSize(strtab_len, false).)
    while len(out) % 2:
        out.append(0)
    voli = bytearray()
    for (name, block_off, size), str_off in zip(items, str_offsets):
        voli += struct.pack("<IiIIB", _name_id(name), str_off, block_off, size, 0)
    out += b"voli" + struct.pack("<I", len(voli)) + voli

    struct.pack_into("<I", out, 4, string_block_off)
    return bytes(out)


# ------------------------------------------------------------- driver
def main():
    ap = argparse.ArgumentParser(description="Build a Tribes interior .vol from an OBJ (PoC).")
    ap.add_argument("obj", help="input .obj")
    ap.add_argument("-o", "--out", help="output .vol (default: <name>.vol next to the obj)")
    ap.add_argument("--name", help="interior base name (default: obj stem)")
    ap.add_argument("--dig", help="use a prebuilt .dig (real BSP, from objbuild.js) "
                                  "instead of the empty-BSP fallback")
    ap.add_argument("--dil", help="prebuilt lighting .dil (from objbuild.js) to bundle "
                                  "and reference (needed for the engine to load it)")
    ap.add_argument("--texdir", help="directory of <Material>.bmp textures (from objtex.py) "
                                     "to pack into the vol so materials resolve")
    args = ap.parse_args()

    verts, uvs, faces, mats = parse_obj(args.obj)
    name = args.name or os.path.splitext(os.path.basename(args.obj))[0]
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.obj)), name + ".vol")

    dig_name = f"{name}-00.dig"
    dml_name = f"{name}.dml"
    dis_name = f"{name}.dis"
    dil_name = f"{name}-000.dil"

    if args.dig:
        with open(args.dig, "rb") as f:
            dig = f.read()
        bsp = "real BSP (prebuilt)"
    else:
        dig = build_dig(verts, uvs, faces)
        bsp = "EMPTY BSP (fallback; run objbuild.js for a real one)"
    dml = build_dml(mats)

    entries = [(dis_name, None), (dig_name, dig), (dml_name, dml)]
    if args.dil:
        with open(args.dil, "rb") as f:
            entries.append((dil_name, f.read()))
        dis = build_dis(name, dig_name, dml_name, dil_name)
    else:
        dis = build_dis(name, dig_name, dml_name)
    entries[0] = (dis_name, dis)

    # pack material textures (so Body.bmp/Goggles.bmp etc. resolve from this vol)
    if args.texdir:
        import glob
        for bp in sorted(glob.glob(os.path.join(args.texdir, "*.bmp"))):
            with open(bp, "rb") as f:
                entries.append((os.path.basename(bp), f.read()))

    vol = build_vol(entries)

    with open(out, "wb") as f:
        f.write(vol)

    print(f"wrote {out}")
    print(f"  {len(verts)} verts, {len(uvs)} UVs, {len(faces)} faces, {len(mats)} materials")
    print(f"  packed: " + ", ".join(f"{n} ({len(d)}b)" for n, d in entries))
    print(f"  geometry: {bsp}" + ("  + lighting .dil" if args.dil else "  (no .dil)"))


if __name__ == "__main__":
    main()
