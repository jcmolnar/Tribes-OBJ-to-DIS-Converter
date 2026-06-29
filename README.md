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
              node objbuild.js    ── engine BSP + PVS + lighting ──►  X-00.dig / X-000.dil
                    │                 (--box / --nocollide / --carve / --probe)
                    ▼
              objtex.py           ── per-material textures (.bmp)  ──►  tex/*.bmp
                    │
                    ▼
              obj2vol.py          ── pack .dis/.dig/.dml + textures ─►  X.vol   ✅ load in-game
```

### Prop (don't enter)
```
node objbuild.js  model.obj  X-00.dig X-000.dil  --box     # full-detail render + cheap box collision
python objtex.py  model.obj  --outdir tex                  # textures from the material colors/maps
python obj2vol.py model.obj  --name X --dig X-00.dig --dil X-000.dil --texdir tex -o X.vol
```

### Walk-in building (enter it)
```
python objvoxel.py model.obj vox.obj --res 112 --carve "x0,y0,z0,x1,y1,z1"  # blocky interior + doorway
node   objbuild.js vox.obj   X-00.dig X-000.dil --probe=0,0,40 --probe=...   # NORMAL build; verify solid
python objtex.py / remap textures, then:
python obj2vol.py  vox.obj   --name X --dig X-00.dig --dil X-000.dil --texdir tex -o X.vol
```

Only requirements: **Python 3** and **Node.js** (for the prebuilt `objbuild.js`). No emscripten, no engine build.

---

## Tools

| file | role |
|------|------|
| `obj2vol.py`    | Pack `.dis`+`.dig`+`.dml`(+textures) into a `.vol`. WORD-aligned directory (engine-correct). `--texdir` packs `<Material>.bmp` textures. |
| `objvoxel.py`   | Voxelize an OBJ into a **blocky walk-in interior**: surface-voxelize → flood-fill solidify (fix hollow walls) → boundary-surface extraction (cull internal faces) → `--carve` doorways. |
| `objsimplify.py`| Vertex-cluster decimation + weld + degenerate-face drop (shrink a dense mesh, preserving materials/UVs). |
| `objtex.py`     | Generate a Tribes PBMP per material (needs a world palette: pass `--ppl <Tribes>/base/lushWorld.vol:lush.day.ppl`, or set `TRIBES_DIR`) — `map_Kd` image → palette-quantized, or a solid swatch from `Kd`. |
| `textures.py`   | PBMP / Windows-DIB + `.ppl` (PL98 multipalette) read/write. |
| `volread.py`    | PVOL (`.vol`) reader (used to pull palettes / real textures). |
| `objbuild.js` + `objbuild.wasm` | **The real BSP step — prebuilt, run with Node** (any OS, no engine build needed). The 1998 engine compiled to WebAssembly; drives `ITRBSPBuild::buildTree` + `ITRPortal::buildPVS` + `ITRBasicLighting::light` and a ported `ITR3DMImport::importFromArrays`. Flags: `--box`, `--nocollide`, `--carve`, `--collider`, `--probe`. |
| `objbuild.cpp`, `build-objbuild.ps1` | *(optional, reference only)* the C++ source of the harness and the script that compiled it. **You don't need these** — `objbuild.js` is already built. They're here only if you ever want to see or modify how it was made. |

### Real BSP works out of the box
The detailed BSP/PVS/lighting comes from the actual 1998 engine, **already compiled for you** into `objbuild.js` + `objbuild.wasm` (included, ~1.6 MB). Just `node objbuild.js …` — **that's all you need. No engine source, no C++, no emscripten.** (`obj2vol.py` on its own writes an *empty BSP* — fine for round-tripping geometry, but use the included `objbuild.js` for anything you want the game to actually render and collide.)

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

The Python tools are stdlib-only (no Pillow). `objbuild.js`/`.wasm` is the Darkstar engine prebuilt to WebAssembly — it runs as-is with Node; no engine source or build toolchain required. Starsiege Tribes has been freeware since 2015. Open source, provided as-is.
