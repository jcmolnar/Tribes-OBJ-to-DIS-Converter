#!/usr/bin/env python3
# objsimplify.py - decimate + weld a Wavefront OBJ so the interior it builds
# has FEW enough distinct planes that the Tribes server's collision-clip walk
# (ITRCollision::collideBox, a hard 400-node-per-player-sphere cap) never
# overflows. A rounded mesh (e.g. an FBX character) has ~1 distinct plane per
# facet, so a 944-face crewmate makes ~1700 BSP nodes and crashes an UNMODIFIED
# server the instant a player touches a concavity. Reducing the facet count
# cuts the distinct-plane count ~proportionally.
#
# Method: vertex-cluster decimation. Snap every vertex to a coarse grid; all
# verts in a cell collapse to their centroid (this also WELDS the duplicate /
# near-degenerate verts an FBX->Blender->OBJ export leaves behind). Faces whose
# corners collapse to fewer than 3 distinct verts are dropped (degenerate).
# Materials + per-corner UVs are preserved, so the render still looks like the
# model -- just chunkier. Bigger --cell = fewer planes = safer + blockier.
#
#   python tools/objsimplify.py in.obj out.obj [--cell U | --ratio R] [--report]
#
# Then run the normal pipeline on out.obj:
#   node build/objbuild.js out.obj X-00.dig X-000.dil
#   python tools/obj2vol.py out.obj --name X --dig X-00.dig --dil X-000.dil -o X.vol
#
# Tuning: watch objbuild's "buildTree: nodes=N". Aim well under the cap; since
# the cap is LOCAL (planes within one player sphere), getting total nodes to a
# few hundred is a safe margin. This tool also prints an estimate of the number
# of DISTINCT face planes (a proxy for BSP node count) before/after.

import sys
import math
import argparse
from collections import defaultdict


def parse_obj(path):
    verts = []          # list of (x,y,z)
    uvs = []            # list of (u,v)
    faces = []          # list of (material, [(vi, ti_or_None), ...])  0-based
    header = []         # mtllib / o lines to preserve
    cur_mat = None
    with open(path, "r") as fp:
        for line in fp:
            s = line.strip()
            if not s:
                continue
            if s.startswith("v "):
                p = s.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
            elif s.startswith("vt "):
                p = s.split()
                uvs.append((float(p[1]), float(p[2])))
            elif s.startswith("vn "):
                pass  # normals are recomputed by the BSP builder; drop them
            elif s.startswith("mtllib") or s.startswith("o "):
                header.append(s)
            elif s.startswith("usemtl"):
                cur_mat = s[6:].strip()
            elif s.startswith("f "):
                corners = []
                for tok in s.split()[1:]:
                    bits = tok.split("/")
                    vi = int(bits[0]) - 1
                    ti = None
                    if len(bits) >= 2 and bits[1] != "":
                        ti = int(bits[1]) - 1
                    corners.append((vi, ti))
                if len(corners) >= 3:
                    faces.append((cur_mat, corners))
    return verts, uvs, faces, header


def bbox(verts):
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def face_plane_key(verts, corners, quant):
    # Normalized plane (nx,ny,nz,d) quantized -> a hashable key. Used only to
    # ESTIMATE distinct-plane count (a proxy for BSP node count).
    p0 = verts[corners[0][0]]
    p1 = verts[corners[1][0]]
    p2 = verts[corners[2][0]]
    ux, uy, uz = p1[0]-p0[0], p1[1]-p0[1], p1[2]-p0[2]
    vx, vy, vz = p2[0]-p0[0], p2[1]-p0[1], p2[2]-p0[2]
    nx = uy*vz - uz*vy
    ny = uz*vx - ux*vz
    nz = ux*vy - uy*vx
    L = math.sqrt(nx*nx + ny*ny + nz*nz)
    if L < 1e-12:
        return None  # degenerate
    nx, ny, nz = nx/L, ny/L, nz/L
    d = -(nx*p0[0] + ny*p0[1] + nz*p0[2])
    # Fold sign so a plane and its flip hash the same (BSP nodes are undirected)
    if (nx, ny, nz) < (-nx, -ny, -nz):
        nx, ny, nz, d = -nx, -ny, -nz, -d
    q = 1.0 / quant
    return (round(nx*q), round(ny*q), round(nz*q), round(d*q))


def distinct_planes(verts, faces, quant):
    keys = set()
    for _mat, corners in faces:
        k = face_plane_key(verts, corners, quant)
        if k is not None:
            keys.add(k)
    return len(keys)


def simplify(verts, uvs, faces, cell):
    # Cluster every vertex to a grid cell; representative = centroid of members.
    cell_members = defaultdict(list)
    for i, (x, y, z) in enumerate(verts):
        key = (math.floor(x / cell), math.floor(y / cell), math.floor(z / cell))
        cell_members[key].append(i)

    # Assign a new index per cell, compute centroid, build old->new map.
    new_verts = []
    old_to_new = [0] * len(verts)
    for key, members in cell_members.items():
        nidx = len(new_verts)
        sx = sum(verts[m][0] for m in members) / len(members)
        sy = sum(verts[m][1] for m in members) / len(members)
        sz = sum(verts[m][2] for m in members) / len(members)
        new_verts.append((sx, sy, sz))
        for m in members:
            old_to_new[m] = nidx

    # Remap faces, collapse repeated/adjacent-duplicate corners, drop degenerate.
    new_faces = []
    for mat, corners in faces:
        out = []
        for (vi, ti) in corners:
            nv = old_to_new[vi]
            if out and out[-1][0] == nv:
                continue  # adjacent duplicate after welding
            out.append((nv, ti))
        # close-loop duplicate (first == last)
        if len(out) > 1 and out[0][0] == out[-1][0]:
            out.pop()
        if len(set(c[0] for c in out)) >= 3:
            new_faces.append((mat, out))

    return new_verts, new_faces


def write_obj(path, verts, uvs, faces, header):
    # Re-index only the UVs actually referenced, keep materials grouped.
    used_uv = {}
    for _mat, corners in faces:
        for (_vi, ti) in corners:
            if ti is not None and ti not in used_uv:
                used_uv[ti] = len(used_uv)
    with open(path, "w") as fp:
        fp.write("# objsimplify.py - decimated/welded for Tribes collision\n")
        for h in header:
            fp.write(h + "\n")
        for (x, y, z) in verts:
            fp.write("v %.6f %.6f %.6f\n" % (x, y, z))
        # write UVs in new order
        inv = [0] * len(used_uv)
        for old, new in used_uv.items():
            inv[new] = old
        for old in inv:
            u, v = uvs[old]
            fp.write("vt %.6f %.6f\n" % (u, v))
        cur_mat = None
        for mat, corners in faces:
            if mat != cur_mat:
                fp.write("usemtl %s\n" % (mat if mat else "default"))
                cur_mat = mat
            parts = ["f"]
            for (vi, ti) in corners:
                if ti is not None:
                    parts.append("%d/%d" % (vi + 1, used_uv[ti] + 1))
                else:
                    parts.append("%d" % (vi + 1))
            fp.write(" ".join(parts) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Decimate+weld an OBJ for Tribes collision safety.")
    ap.add_argument("infile")
    ap.add_argument("outfile")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--cell", type=float, default=None,
                   help="grid cell size in OBJ units (smaller = more detail)")
    g.add_argument("--ratio", type=float, default=0.03,
                   help="cell size as a fraction of the bbox diagonal (default 0.03)")
    ap.add_argument("--report", action="store_true",
                    help="print plane-count estimate and exit without writing")
    args = ap.parse_args()

    verts, uvs, faces, header = parse_obj(args.infile)
    bmin, bmax = bbox(verts)
    diag = math.sqrt(sum((bmax[i] - bmin[i]) ** 2 for i in range(3)))
    quant = diag * 0.005  # plane-estimate quantization (~0.5% of size)

    cell = args.cell if args.cell is not None else diag * args.ratio

    p_before = distinct_planes(verts, faces, quant)
    print("input : %d verts, %d uvs, %d faces" % (len(verts), len(uvs), len(faces)))
    print("bbox  : (%.3f,%.3f,%.3f)..(%.3f,%.3f,%.3f)  diag=%.3f" %
          (bmin[0], bmin[1], bmin[2], bmax[0], bmax[1], bmax[2], diag))
    print("planes: ~%d distinct (proxy for BSP node count)" % p_before)
    print("cell  : %.4f units" % cell)

    if args.report:
        return

    nv, nf = simplify(verts, uvs, faces, cell)
    p_after = distinct_planes(nv, nf, quant)
    print("output: %d verts, %d faces  (planes ~%d)" % (len(nv), len(nf), p_after))
    if p_before:
        print("reduction: faces %.0f%%, planes %.0f%%" %
              (100.0 * (1 - len(nf) / max(1, len(faces))),
               100.0 * (1 - p_after / max(1, p_before))))
    write_obj(args.outfile, nv, uvs, nf, header)
    print("wrote %s" % args.outfile)


if __name__ == "__main__":
    main()
