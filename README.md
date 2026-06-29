# Tribes OBJ → DIS Converter

Turn an arbitrary **Wavefront `.obj`** (e.g. an FBX exported from Blender) into a working **Starsiege Tribes / Darkstar** interior — a `.vol` archive containing `.dis` + `.dig` + `.dml` (+ packed textures) that the 1998 engine loads, renders, lights, and collides against.

Two kinds of output, both proven in-game:

- **Props** — statues, vehicles, decorations you view and bump into from outside.
- **Walk-in buildings** — castles, bases, bunkers you actually enter (blocky but real interiors, with carved doorways).

> Reverse direction (DIS → OBJ extraction) lives in a separate repo: **Tribes-DIS-to-OBJ**.

---

## The pipeline

```
            ┌─ (objsimplify.py)  decimate/weld a dense mesh        ─┐  optional
 model.obj ─┤                                                       ├─►
            └─ (objvoxel.py)     voxelize → blocky WALK-IN interior ─┘  (buildings only)
                    │
                    ▼
              objbuild(.cpp/.js)  ── engine BSP + PVS + lighting ──►  X-00.dig / X-000.dil
                    │                 (--box / --nocollide / --carve / --probe)
                    ▼
              objtex.py           ── per-material textures (.bmp)  ──►  tex/*.bmp
                    │
                    ▼
              obj2vol.py          ── pack .dis/.dig/.dml + textures ─►  X.vol   ✅ load in-game
```

### Prop (don't enter)
```
objbuild  model.obj  X-00.dig X-000.dil  --box        # full-detail render + cheap box collision
objtex    model.obj  --outdir tex                     # textures from the material colors/maps
obj2vol   model.obj  --name X --dig X-00.dig --dil X-000.dil --texdir tex -o X.vol
```

### Walk-in building (enter it)
```
objvoxel  model.obj  vox.obj  --res 112 --carve "x0,y0,z0,x1,y1,z1"   # blocky interior + a doorway
objbuild  vox.obj    X-00.dig X-000.dil  --probe=0,0,40 --probe=...   # NORMAL build; verify solid
objtex / remap textures, then:
obj2vol   vox.obj    --name X --dig X-00.dig --dil X-000.dil --texdir tex -o X.vol
```

---

## Tools

| file | role |
|------|------|
| `obj2vol.py`    | Pack `.dis`+`.dig`+`.dml`(+textures) into a `.vol`. WORD-aligned directory (engine-correct). `--texdir` packs `<Material>.bmp` textures. |
| `objvoxel.py`   | Voxelize an OBJ into a **blocky walk-in interior**: surface-voxelize → flood-fill solidify (fix hollow walls) → boundary-surface extraction (cull internal faces) → `--carve` doorways. |
| `objsimplify.py`| Vertex-cluster decimation + weld + degenerate-face drop (shrink a dense mesh, preserving materials/UVs). |
| `objtex.py`     | Generate a Tribes PBMP per material — `map_Kd` image → palette-quantized, or a solid swatch from `Kd`. |
| `textures.py`   | PBMP / Windows-DIB + `.ppl` (PL98 multipalette) read/write. |
| `volread.py`    | PVOL (`.vol`) reader (used to pull palettes / real textures). |
| `objbuild.cpp`  | **The real BSP step** — a harness that drives the engine's own `ITRBSPBuild::buildTree` + `ITRPortal::buildPVS` + `ITRBasicLighting::light` and a ported `ITR3DMImport::importFromArrays`. Adds `--box`, `--nocollide`, `--carve`, `--collider`, `--probe`. **Requires the Darkstar engine sources to build — not included here** (see below). |
| `build-objbuild.ps1` | Reference build script for `objbuild` against the engine tree (emscripten → `objbuild.js`, node-runnable). |

### The engine dependency
The detailed BSP/PVS/lighting comes from the actual 1998 engine, via `objbuild`. That engine source is **not** in this repo (legality is gray; Tribes itself is freeware since 2015). You need the Darkstar/Tribes engine tree to compile `objbuild`. **Without it, `obj2vol.py` still works but writes an *empty-BSP* fallback** — fine for round-tripping geometry back through the extractor, but the live engine won't render/cull a complex interior from it. The Python tools (voxelize, simplify, texture, pack) are fully standalone.

---

## Hard-won knowledge (read this before you debug for hours)

These are the non-obvious rules that make the difference between "loads and renders" and "crashes / invisible / untextured":

- **VOL directory alignment is WORD (2-byte), not 4.** The engine finds the file index (`voli`) at `vols_start + 8 + alignSize(strtab_len, WORD)`. A 4-byte pad over-shoots for many string-table lengths → 0 files indexed → *"Could not load interior."* (`obj2vol.py` does this correctly.)
- **Cull internal faces, or the solid inverts.** Emitting whole boxes buries the faces shared between adjacent boxes inside the solid → the BSP classifies the interior as solid and the walls as empty (you walk through walls and get stuck in open space / "just sky"). `objvoxel.py` extracts only the boundary surface.
- **Detail ceiling = ~2048 surfaces.** The interior texture-handle cache is 2048 entries; beyond that, surfaces render untextured. Keep `objvoxel`'s reported boundary-quad count under ~1900.
- **A material must resolve or the client crashes on contact.** A `.bmp` not found in any mounted vol → renders white *and* the client derefs the NULL material the instant you touch the surface → crash. Either name a stock texture (resolves from the game vols) or pack a real one with `--texdir`.
- **`paletteIndex 503`** is the "interior structures" palette — identical across every world `.ppl` and always loaded. Use it for interior textures. Textures are palettized 8-bit PBMP, power-of-2, with a full mip chain (the renderer reads mip levels unclamped).
- **Validate without launching the game:** `objbuild --probe=x,y,z` (in scaled units) prints solid/empty per point — confirm interior=empty, walls=solid, doorway=empty before deploying.
- **Walls = inward normals** for a solid (objvoxel handles winding automatically); **subtractive/underground** pieces (carve a void out of solid) are the inverse — only the inner faces exist.

---

## Notes

Pure Python 3, standard library only (no Pillow). Starsiege Tribes has been freeware since 2015; this is original tooling for content creation / preservation and contains no game engine source. Provided as-is.
