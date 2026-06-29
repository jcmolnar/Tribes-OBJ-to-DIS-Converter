// WASM-PORT: OBJ -> .dig interior compiler harness (node-runnable).
// Parses a Wavefront OBJ, fills an ITRGeometry via ITR3DMImport::importFromArrays,
// runs the engine's real BSP compiler (ITRBSPBuild::buildTree) + PVS
// (ITRPortal::buildPVS), then writes a .dig with ITRGeometry::fileStore.
//
//   node build\objbuild.js <in.obj> <out.dig>
//
// This is the piece obj2vol.py lacked: a real BSP instead of an empty one.
// Uses the engine's Vector<> (no STL: <vector>/<locale> breaks under -D_WIN32).

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <ml.h>
#include <tvector.h>
#include <filstrm.h>
#include <itrgeometry.h>
#include <itr3dmimport.h>
#include <itrbsp.h>
#include <itrportal.h>
#include <itrbit.h>
#include <tplane.h>
#include <itrlighting.h>
#include <itrbasiclighting.h>

int main(int argc, char** argv)
{
   if (argc < 3) {
      printf("usage: objbuild <in.obj> <out.dig> [out.dil] [--box]\n");
      return 1;
   }
   const char* objPath = argv[1];
   const char* digPath = argv[2];
   const char* dilPath = NULL;
   // --box: HYBRID collision. Keep the full-detail render geometry but replace
   // the BSP nodeList (collision traversal) with a 6-plane axis-aligned box, so
   // collision is a cheap convex box that can NEVER trip the server/client
   // ITRCollision::collideBox 400-node clip cap -- yet the interior still RENDERS
   // the detailed mesh (the render path uses externalLeaf()/reserved leaves/PVS
   // when viewed from outside the bbox, none of which touch nodeList). For
   // complex/non-convex props on an UNMODIFIED (unpatchable) server+client.
   // --nocollide: like --box but the box has NO solid region, so the interior
   // RENDERS at full detail yet collides with nothing (walk-through decoration).
   // For complex buildings (e.g. an FBX castle) you want to roam inside but whose
   // real per-wall BSP would overflow the collision clip cap on the immutable server.
   bool boxCollision = false;
   bool noCollide = false;
   for (int i = 3; i < argc; i++) {
      if (strcmp(argv[i], "--box") == 0) boxCollision = true;
      else if (strcmp(argv[i], "--nocollide") == 0) { boxCollision = true; noCollide = true; }
      else if (strncmp(argv[i], "--probe=", 8) == 0) { /* handled after buildPVS */ }
      else dilPath = argv[i];
   }

   FILE* fp = fopen(objPath, "r");
   if (!fp) { printf("cannot open %s\n", objPath); return 1; }

   Vector<Point3F> points;
   Vector<Point2F> texcoords;
   Vector<int> faceMat, faceVCount, facePointIdx, faceTexIdx;

   static char matNames[256][64];
   int nMats = 0, curMat = 0;

   char line[1024];
   while (fgets(line, sizeof(line), fp)) {
      if (line[0] == 'v' && line[1] == ' ') {
         Point3F p; sscanf(line + 2, "%f %f %f", &p.x, &p.y, &p.z);
         points.push_back(p);
      } else if (line[0] == 'v' && line[1] == 't') {
         Point2F t; sscanf(line + 3, "%f %f", &t.x, &t.y);
         texcoords.push_back(t);
      } else if (strncmp(line, "usemtl", 6) == 0) {
         char name[64]; name[0] = 0; sscanf(line + 6, "%63s", name);
         int idx = -1;
         for (int i = 0; i < nMats; i++) if (strcmp(matNames[i], name) == 0) { idx = i; break; }
         if (idx < 0 && nMats < 256) { idx = nMats; strncpy(matNames[nMats++], name, 63); }
         curMat = (idx < 0) ? 0 : idx;
      } else if (line[0] == 'f' && line[1] == ' ') {
         int vc = 0;
         char* tok = strtok(line + 2, " \t\r\n");
         while (tok) {
            int vi = 0, ti = 0;
            if (sscanf(tok, "%d/%d", &vi, &ti) >= 1) {
               facePointIdx.push_back(vi - 1);                 // OBJ is 1-based
               faceTexIdx.push_back(ti > 0 ? ti - 1 : -1);
               vc++;
            }
            tok = strtok(NULL, " \t\r\n");
         }
         if (vc >= 3) { faceMat.push_back(curMat); faceVCount.push_back(vc); }
      }
   }
   fclose(fp);
   if (nMats == 0) nMats = 1;

   printf("OBJ: %d verts, %d uvs, %d faces, %d materials\n",
          points.size(), texcoords.size(), faceVCount.size(), nMats);

   ITRGeometry geometry;
   geometry.textureScale = 16.0f;
   // Anchor the persistent registrar (ITRGeometry::t) so --gc-sections keeps it
   // and its registering constructor runs -- else fileStore can't look up the
   // class name (UnregisteredClassError). getClass() returns &t.
   if (geometry.getClass() == NULL) return 1;
   Vector<UInt32> volumeMasks;

   ITR3DMImport::importFromArrays(
      points.address(), points.size(),
      texcoords.address(), texcoords.size(),
      faceMat.address(), faceVCount.address(),
      facePointIdx.address(), faceTexIdx.address(), faceVCount.size(),
      &geometry, &volumeMasks);

   printf("imported: surfaces=%d points=%d planes=%d\n",
          geometry.surfaceList.size(), geometry.point3List.size(),
          geometry.planeList.size());

   ITRBSPBuild::buildTree(&geometry, &volumeMasks, false);
   printf("buildTree: nodes=%d solidLeaves=%d emptyLeaves=%d bitList=%d\n",
          geometry.nodeList.size(), geometry.solidLeafList.size(),
          geometry.emptyLeafList.size(), geometry.bitList.size());

   ITRPortal::buildPVS(&geometry);
   printf("buildPVS: bitList=%d (PVS appended)\n", geometry.bitList.size());

   // WASM-PORT probe: classify points so we KNOW whether the solid region is the
   // walls (correct) or inverted (the open space / courtyard). Pass --probe
   // "x,y,z" (repeatable) in MODEL units; prints solid=1/0 per point.
   for (int i = 3; i < argc; i++) {
      if (strncmp(argv[i], "--probe=", 8) == 0) {
         Point3F p; if (sscanf(argv[i] + 8, "%f,%f,%f", &p.x, &p.y, &p.z) == 3) {
            int leaf = geometry.findLeaf(p);
            ITRGeometry::BSPLeafWrap w(&geometry, leaf);
            printf("PROBE (%.3f,%.3f,%.3f) -> leaf=%d solid=%d\n",
                   p.x, p.y, p.z, leaf, (int)w.isSolid());
         }
      }
   }

   if (boxCollision) {
      // ---- HYBRID: swap collision (nodeList) for a 6-plane box ----------------
      // The render structures (surfaceList/leaves/PVS/reserved leaves/bitList)
      // built above are LEFT UNTOUCHED -> outside-the-bbox rendering (the prop
      // case) is byte-identical to a normal full-detail interior. We only:
      //   (1) append 6 inward-facing AABB planes,
      //   (2) append 6 box surfaces on those planes,
      //   (3) repurpose the FIRST solid leaf to hold those 6 box surfaces (so
      //       the collision response finds a real box face per plane), and
      //   (4) replace nodeList with a 6-node chain forming the box solid volume.
      // No leaf-LIST sizes change, so every existing leaf index (PVS, children)
      // stays valid. collideBox now visits <=6 nodes + 1 solid leaf -> can never
      // approach the 400 cap, on server OR client, regardless of mesh detail.
      const Box3F bb = geometry.box;          // set by buildTree (detailed bbox)

      int planeBase = geometry.planeList.size();
      geometry.planeList.push_back(TPlaneF(-1.0f, 0.0f, 0.0f,  bb.fMax.x)); // +X face, inward -X
      geometry.planeList.push_back(TPlaneF( 1.0f, 0.0f, 0.0f, -bb.fMin.x)); // -X face, inward +X
      geometry.planeList.push_back(TPlaneF( 0.0f,-1.0f, 0.0f,  bb.fMax.y));
      geometry.planeList.push_back(TPlaneF( 0.0f, 1.0f, 0.0f, -bb.fMin.y));
      geometry.planeList.push_back(TPlaneF( 0.0f, 0.0f,-1.0f,  bb.fMax.z));
      geometry.planeList.push_back(TPlaneF( 0.0f, 0.0f, 1.0f, -bb.fMin.z));

      int surfBase = geometry.surfaceList.size();
      for (int i = 0; i < 6; i++) {
         ITRGeometry::Surface s;
         memset(&s, 0, sizeof(s));
         s.type             = ITRGeometry::Surface::Material;
         s.material         = 0;                 // any valid material (first one)
         s.planeFront       = 1;
         s.planeIndex       = (UInt16)(planeBase + i);
         s.visibleToOutside = 0;                 // collision-only; never rendered
         s.vertexCount      = 0;                 // pickSurface only reads planeIndex
         s.pointCount       = 0;
         geometry.surfaceList.push_back(s);
      }

      // (3) repurpose solid leaf 0 (absolute leaf index = ReservedLeafEntries)
      ITRGeometry::BSPLeafSolid& sl = geometry.solidLeafList[0];
      {
         ITRBitVector sbv;
         for (int i = 0; i < 6; i++) sbv.set(surfBase + i);
         sl.surfaceIndex = geometry.bitList.size();
         sl.surfaceCount = sbv.compress(&geometry.bitList);
         ITRBitVector pbv;
         for (int i = 0; i < 6; i++) pbv.set(planeBase + i);
         sl.planeIndex = geometry.bitList.size();
         sl.planeCount = pbv.compress(&geometry.bitList);
      }

      // (4) replace nodeList: inward planes => box interior is FRONT of all 6.
      // --box: the innermost region is SOLID (player bumps the box). --nocollide:
      // it's EMPTY too, so no solid leaf is ever reached -> collide() returns
      // nothing -> walk-through (full render, zero collision, zero crash).
      const int SOLID = ITRGeometry::ReservedLeafEntries; // first solid leaf abs idx
      const int EMPTY = 0;                                 // reserved empty leaf 0
      int inside = noCollide ? -(EMPTY + 1) : -(SOLID + 1);
      geometry.nodeList.clear();
      for (int i = 0; i < 6; i++) {
         ITRGeometry::BSPNode n;
         n.planeIndex = (UInt16)(planeBase + i);
         n.front = (i < 5) ? (Int16)(i + 1) : (Int16)inside;  // deeper / solid|empty
         n.back  = (Int16)(-(EMPTY + 1));                      // outside slab
         n.fill  = 0;
         geometry.nodeList.push_back(n);
      }
      printf("%s: nodeList=6 (AABB), planes %d..%d, inside=%s\n",
             noCollide ? "no-collision" : "box-collision",
             planeBase, planeBase + 5, noCollide ? "empty(walk-through)" : "solidLeaf[0]=box");
   }

   // Write the .dig directly: a PERS wrapper (header) + ITRGeometry's own write()
   // for the body. This avoids store()'s class-registry lookup (which the minimal
   // harness can't satisfy) while producing byte-identical output to the engine.
   FILE* out = fopen(digPath, "wb");
   if (!out) { printf("cannot open %s for write\n", digPath); return 1; }

   // body size = exactly what ITRGeometry::write emits (see itrgeometry.cpp)
   typedef ITRGeometry G;
   int bodySize =
        4 + 4 + (int)sizeof(Box3F) + 9 * 4
      + geometry.surfaceList.size()   * (int)sizeof(G::Surface)
      + geometry.nodeList.size()      * (int)sizeof(G::BSPNode)
      + geometry.solidLeafList.size() * (int)sizeof(G::BSPLeafSolid)
      + geometry.emptyLeafList.size() * (int)sizeof(G::BSPLeafEmpty)
      + geometry.bitList.size()       * 1
      + geometry.vertexList.size()    * (int)sizeof(G::Vertex)
      + geometry.point3List.size()    * (int)sizeof(Point3F)
      + geometry.point2List.size()    * (int)sizeof(Point2F)
      + geometry.planeList.size()     * (int)sizeof(TPlaneF)
      + 4 + 4;
   // PERS block: "PERS" + blocksize + namesize(2) + name(12) + version(4) + body
   unsigned int blocksize = 2 + 12 + 4 + bodySize;

   fwrite("PERS", 1, 4, out);
   fwrite(&blocksize, 4, 1, out);
   unsigned short nsz = 11; fwrite(&nsz, 2, 1, out);
   fwrite("ITRGeometry\0", 1, 12, out);                 // 11 chars + 1 pad/null
   int ver = 7; fwrite(&ver, 4, 1, out);

   FileWStream fws;                                       // engine stream wrapper
   // ITRGeometry::write needs a StreamIO; write the body via the engine to a temp
   // then copy. Simpler: write the arrays here in the exact read() order.
   Int32 buildId = geometry.buildId; fwrite(&buildId, 4, 1, out);
   float tscale = geometry.textureScale; fwrite(&tscale, 4, 1, out);
   fwrite(&geometry.box, sizeof(Box3F), 1, out);
   Int32 n;
   n = geometry.surfaceList.size();   fwrite(&n, 4, 1, out);
   Int32 nNode = geometry.nodeList.size();
   Int32 nSolid = geometry.solidLeafList.size();
   Int32 nEmpty = geometry.emptyLeafList.size();
   Int32 nBit = geometry.bitList.size();
   Int32 nVert = geometry.vertexList.size();
   Int32 nP3 = geometry.point3List.size();
   Int32 nP2 = geometry.point2List.size();
   Int32 nPl = geometry.planeList.size();
   fwrite(&nNode, 4, 1, out); fwrite(&nSolid, 4, 1, out); fwrite(&nEmpty, 4, 1, out);
   fwrite(&nBit, 4, 1, out); fwrite(&nVert, 4, 1, out); fwrite(&nP3, 4, 1, out);
   fwrite(&nP2, 4, 1, out); fwrite(&nPl, 4, 1, out);
   fwrite(geometry.surfaceList.address(),   sizeof(G::Surface),      geometry.surfaceList.size(),   out);
   fwrite(geometry.nodeList.address(),      sizeof(G::BSPNode),      geometry.nodeList.size(),      out);
   fwrite(geometry.solidLeafList.address(), sizeof(G::BSPLeafSolid), geometry.solidLeafList.size(), out);
   fwrite(geometry.emptyLeafList.address(), sizeof(G::BSPLeafEmpty), geometry.emptyLeafList.size(), out);
   fwrite(geometry.bitList.address(),       1,                       geometry.bitList.size(),       out);
   fwrite(geometry.vertexList.address(),    sizeof(G::Vertex),       geometry.vertexList.size(),    out);
   fwrite(geometry.point3List.address(),    sizeof(Point3F),         geometry.point3List.size(),    out);
   fwrite(geometry.point2List.address(),    sizeof(Point2F),         geometry.point2List.size(),    out);
   fwrite(geometry.planeList.address(),     sizeof(TPlaneF),         geometry.planeList.size(),     out);
   Int32 hml = geometry.highestMipLevel; fwrite(&hml, 4, 1, out);
   UInt32 flags = geometry.testFlag(ITRGeometry::LowDetailInterior) ? 1u : 0u;
   fwrite(&flags, 4, 1, out);
   fclose(out);
   (void)fws;

   printf("wrote %s (%u-byte PERS block)\n", digPath, blocksize + 8);

   // ---- lighting: build a .dil so the .dis can reference a valid light state ----
   // (an interior with a light state but no .dil crashes the server on load).
   if (dilPath) {
      ITRBasicLighting::m_ambientIntensity = ColorF(1.0f, 1.0f, 1.0f);  // full ambient
      ITRBasicLighting::lightScale = 4;
      ITRLighting lighting;
      ITRBasicLighting::LightList lights;          // empty -> default/ambient lighting
      ITRBasicLighting::MaterialPropList props;    // empty
      ITRBasicLighting::light(geometry, lights, props, &lighting);

      FileWStream dil;
      if (!dil.open(dilPath)) { printf("cannot open %s\n", dilPath); return 1; }
      DWORD pers = FOURCC('P','E','R','S'); dil.write(pers);
      Int32 szpos = dil.getPosition(); DWORD zero = 0; dil.write(zero);
      short nm = 11; dil.write(nm);
      dil.write(12, "ITRLighting\0");               // 11 chars + 1 null
      int dver = 7; dil.write(dver);
      lighting.write(dil, 7, 0);
      Int32 endp = dil.getPosition();
      dil.setPosition(szpos);
      DWORD bs = (DWORD)(endp - szpos - 4);
      dil.write(bs);
      dil.close();
      printf("wrote %s (lighting .dil, %d bytes)\n", dilPath, (int)endp);
   }
   return 0;
}
