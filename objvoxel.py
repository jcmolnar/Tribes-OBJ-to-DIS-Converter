#!/usr/bin/env python3
"""
objvoxel.py - Turn an arbitrary OBJ (e.g. an FBX building) into a BLOCKY but
REAL Tribes interior you can walk INTO: render and collision are the same voxel
geometry, so it renders correctly from inside AND outside (unlike the
--box/--nocollide prop trick, which only looks right viewed from outside the
bbox because render-from-inside uses the same nodeList collision walks).

Why blocky/unified instead of "detailed mesh + separate collider": interior
collision AND render-from-inside both walk geometry->nodeList (interiorShape.cpp
findLeaf / ITRCollision). You can't give render a detailed BSP and collision a
simple one at the same time -- so to walk inside and still see walls, the walls
must BE a sane low-poly (voxel) BSP. This is exactly how ncity stays cheap:
big flat brushes, low local plane density.

Method: SURFACE-voxelize (mark every grid cell a triangle passes through as
solid wall -> 1-cell-thick walls, interior + exterior left empty = walkable;
openings wider than a cell stay open). Greedy-merge same-material solid cells
into boxes (few big boxes = sparse BSP = no collision-cap / PVS-size blowup).
Emit each box as 6 OUTWARD quads (the build pipeline flips winding -> inward =
solid) with the cell's material, so objtex still colors them.

  python tools/objvoxel.py in.obj out.obj [--cell U | --res N] [--report]

Then the NORMAL pipeline (real collision, NOT --box):
  objbuild out.obj m-00.dig m-000.dil
  objtex   in.obj  --outdir tex                 (materials from the ORIGINAL mtl)
  obj2vol  out.obj --name X --dig .. --dil .. --texdir tex -o X.vol
"""

import sys
import math
import argparse


def parse_obj(path):
    verts = []
    tris = []        # (mat, (i0,i1,i2))  triangulated, 0-based
    cur = "default"
    for line in open(path):
        p = line.split()
        if not p:
            continue
        if p[0] == "v":
            verts.append((float(p[1]), float(p[2]), float(p[3])))
        elif p[0] == "usemtl":
            cur = p[1]
        elif p[0] == "f":
            idx = [int(t.split("/")[0]) - 1 for t in p[1:]]
            for k in range(1, len(idx) - 1):       # fan-triangulate
                tris.append((cur, (idx[0], idx[k], idx[k + 1])))
    return verts, tris


def bbox(verts):
    xs = [v[0] for v in verts]; ys = [v[1] for v in verts]; zs = [v[2] for v in verts]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def voxelize(verts, tris, cell, origin):
    """Surface voxelization: mark cells any triangle passes through. Returns
    dict (i,j,k)->material. Samples each triangle on a grid finer than a cell."""
    ox, oy, oz = origin
    grid = {}
    for mat, (a, b, c) in tris:
        pa, pb, pc = verts[a], verts[b], verts[c]
        # sample density: longest edge / (cell/2), so no cell is skipped
        e1 = math.dist(pa, pb); e2 = math.dist(pa, pc); e3 = math.dist(pb, pc)
        n = max(2, int(max(e1, e2, e3) / (cell * 0.5)) + 1)
        for ui in range(n + 1):
            for vi in range(n + 1 - ui):
                u = ui / n; v = vi / n
                x = pa[0] + u * (pb[0] - pa[0]) + v * (pc[0] - pa[0])
                y = pa[1] + u * (pb[1] - pa[1]) + v * (pc[1] - pa[1])
                z = pa[2] + u * (pb[2] - pa[2]) + v * (pc[2] - pa[2])
                key = (int((x - ox) // cell), int((y - oy) // cell), int((z - oz) // cell))
                if key not in grid:        # first material wins (stable)
                    grid[key] = mat
    return grid


def solidify(grid):
    """Fill hollow wall cavities and enclosed voids. Surface voxelization only
    marks the thin faces a mesh passes through, so a wall with thickness becomes
    two solid sheets with an EMPTY cavity between -> the player punches the outer
    sheet and gets trapped in the hollow. Fix: flood empty space inward from
    OUTSIDE the model; any non-solid cell the flood can't reach is enclosed
    (wall interior or sealed void) -> make it solid. Rooms reachable through open
    doorways stay reachable -> stay empty (walkable). Doorways are NOT sealed.
    Newly-solid cells inherit a neighboring wall cell's material."""
    if not grid:
        return grid
    xs = [k[0] for k in grid]; ys = [k[1] for k in grid]; zs = [k[2] for k in grid]
    lo = (min(xs) - 1, min(ys) - 1, min(zs) - 1)
    hi = (max(xs) + 1, max(ys) + 1, max(zs) + 1)

    def inb(c):
        return lo[0] <= c[0] <= hi[0] and lo[1] <= c[1] <= hi[1] and lo[2] <= c[2] <= hi[2]

    # BFS the empty exterior from a corner (guaranteed empty padding cell)
    from collections import deque
    outside = set()
    start = lo
    dq = deque([start]); outside.add(start)
    nb6 = [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)]
    while dq:
        c = dq.popleft()
        for d in nb6:
            n = (c[0]+d[0], c[1]+d[1], c[2]+d[2])
            if inb(n) and n not in outside and n not in grid:
                outside.add(n); dq.append(n)

    # any in-bounds non-solid cell not reached from outside is enclosed -> solid
    out = dict(grid)
    filled = 0
    for i in range(lo[0], hi[0] + 1):
        for j in range(lo[1], hi[1] + 1):
            for k in range(lo[2], hi[2] + 1):
                c = (i, j, k)
                if c not in out and c not in outside:
                    # inherit material from a face-neighbor wall cell
                    mat = None
                    for d in nb6:
                        nm = grid.get((i+d[0], j+d[1], k+d[2]))
                        if nm:
                            mat = nm; break
                    out[c] = mat or next(iter(grid.values()))
                    filled += 1
    return out, filled


def greedy_surface(grid, cell, origin, flipwind=False):
    """Extract the BOUNDARY surface of the solid voxel set as merged quads,
    CULLING internal (solid-solid shared) faces. Emitting whole boxes left those
    internal faces buried in the solid mass, which makes buildTree classify the
    solid backwards/degenerate (probed: courtyard solid, walls empty). A clean
    closed boundary surface (like the wood cube, just bigger) classifies right.
    2D greedy-merges coplanar boundary faces per direction to keep the BSP sparse.
    Winding is chosen so the normal faces the EMPTY side; if a probe shows the
    solid inverted, pass flipwind=True. Returns [(corners[4], material), ...]."""
    solid = set(grid.keys())
    ox, oy, oz = origin
    quads = []

    def world(ci, cj, ck):
        return (ox + ci * cell, oy + cj * cell, oz + ck * cell)

    for a in range(3):
        u, v = (a + 1) % 3, (a + 2) % 3
        for s in (+1, -1):
            slices = {}       # a-slice -> {(cu,cv): material} of boundary faces
            for c in solid:
                nb = list(c); nb[a] += s
                if tuple(nb) not in solid:
                    slices.setdefault(c[a], {})[(c[u], c[v])] = grid[c]
            for wa, mask in slices.items():
                used = set()
                for (cu0, cv0) in sorted(mask.keys()):
                    if (cu0, cv0) in used:
                        continue
                    mat = mask[(cu0, cv0)]
                    cu1 = cu0
                    while (cu1 + 1, cv0) in mask and (cu1 + 1, cv0) not in used and mask[(cu1 + 1, cv0)] == mat:
                        cu1 += 1
                    cv1 = cv0; ok = True
                    while ok:
                        for uu in range(cu0, cu1 + 1):
                            cc = (uu, cv1 + 1)
                            if cc not in mask or cc in used or mask[cc] != mat:
                                ok = False; break
                        if ok:
                            cv1 += 1
                    for uu in range(cu0, cu1 + 1):
                        for vv in range(cv0, cv1 + 1):
                            used.add((uu, vv))
                    fa = wa + (1 if s > 0 else 0)     # boundary plane (cell coord)

                    def cor(uu, vv):
                        ci = [0, 0, 0]; ci[a] = fa; ci[u] = uu; ci[v] = vv
                        return world(ci[0], ci[1], ci[2])
                    q = [cor(cu0, cv0), cor(cu1 + 1, cv0), cor(cu1 + 1, cv1 + 1), cor(cu0, cv1 + 1)]
                    # orient so the surface normal points to the EMPTY side (+s axis)
                    if (s > 0) ^ flipwind:
                        q = [q[0], q[3], q[2], q[1]]
                    quads.append((q, mat))
    return quads


def emit_quads(path, quads):
    quads = sorted(quads, key=lambda q: q[1])
    with open(path, "w") as f:
        f.write("# objvoxel.py - boundary surface (internal faces culled)\n")
        f.write("vt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\n")
        for (corners, mat) in quads:
            for (x, y, z) in corners:
                f.write("v %.5f %.5f %.5f\n" % (x, y, z))
        vbase = 1; cur = None
        for (corners, mat) in quads:
            if mat != cur:
                f.write("usemtl %s\n" % mat); cur = mat
            f.write("f %d/1 %d/2 %d/3 %d/4\n" % (vbase, vbase + 1, vbase + 2, vbase + 3))
            vbase += 4


def main():
    ap = argparse.ArgumentParser(description="Voxelize an OBJ into a blocky walk-in Tribes interior.")
    ap.add_argument("infile")
    ap.add_argument("outfile")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--cell", type=float, default=None, help="voxel size in OBJ units")
    g.add_argument("--res", type=int, default=32, help="grid cells across the longest axis (default 32)")
    ap.add_argument("--no-fill", action="store_true",
                    help="skip flood-fill solidify (leave hollow wall shells)")
    ap.add_argument("--carve", action="append", default=[],
                    help="force-empty a box region (carve a doorway) in MODEL units: "
                         "x0,y0,z0,x1,y1,z1 (repeatable). Lets a COARSE/clean grid keep "
                         "solid walls while opening a wide entrance the cell size would seal.")
    ap.add_argument("--flipwind", action="store_true",
                    help="reverse face winding (if a probe shows the solid inverted)")
    ap.add_argument("--report", action="store_true", help="print stats, don't write")
    args = ap.parse_args()

    verts, tris = parse_obj(args.infile)
    bmin, bmax = bbox(verts)
    dims = tuple(bmax[i] - bmin[i] for i in range(3))
    longest = max(dims)
    cell = args.cell if args.cell else longest / args.res
    # pad origin slightly so surface cells aren't clipped
    origin = (bmin[0] - cell * 0.5, bmin[1] - cell * 0.5, bmin[2] - cell * 0.5)

    print("input : %d verts, %d tris" % (len(verts), len(tris)))
    print("bbox  : %.3f x %.3f x %.3f   cell=%.4f  (~%d across longest axis)" %
          (dims[0], dims[1], dims[2], cell, int(longest / cell)))

    grid = voxelize(verts, tris, cell, origin)
    print("surface cells: %d" % len(grid))
    if not args.no_fill:
        grid, filled = solidify(grid)
        print("after flood-fill solidify: %d cells (+%d filled cavities)" % (len(grid), filled))

    # carve doorways: remove solid cells whose center falls inside a carve box
    if args.carve:
        ox, oy, oz = origin
        boxes_c = [tuple(float(v) for v in c.split(",")) for c in args.carve]
        removed = 0
        for key in list(grid.keys()):
            cx = ox + (key[0] + 0.5) * cell
            cy = oy + (key[1] + 0.5) * cell
            cz = oz + (key[2] + 0.5) * cell
            for (x0, y0, z0, x1, y1, z1) in boxes_c:
                if x0 <= cx <= x1 and y0 <= cy <= y1 and z0 <= cz <= z1:
                    del grid[key]; removed += 1; break
        print("carved %d cells from %d doorway box(es)" % (removed, len(boxes_c)))

    quads = greedy_surface(grid, cell, origin, flipwind=args.flipwind)
    print("boundary quads (internal faces culled): %d" % len(quads))

    if args.report:
        return
    emit_quads(args.outfile, quads)
    print("wrote %s" % args.outfile)


if __name__ == "__main__":
    main()
