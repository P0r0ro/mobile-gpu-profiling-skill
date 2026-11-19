# Shader cost: getting real shaders out of the engine and reading malioc

Static analysis only helps if you analyse the shader the engine **actually compiles** (with its
variant defines, lighting code and your material code), not the source you wrote.

## Getting the compiled shaders

| Engine | How |
|---|---|
| **Godot 4 Compatibility (GLES3)** | `scripts/godot_dump_shaders.sh <project> <out> [secs] [scene]`: runs the game on the desktop with Mesa in a GLES 3 context and `MESA_SHADER_DUMP_PATH`. You get `FS_<hash>.glsl` / `VS_<hash>.glsl` as `#version 300 es` (or 320 es), exactly what a GLES phone driver receives. It uses a throw-away copy of the user data (saves untouched, Godot's shader cache bypassed). User identifiers appear with an `m_` prefix (`night` → `m_night`). Needs Mesa (llvmpipe is fine); NVIDIA/AMD proprietary drivers ignore the env var |
| Godot 4 Mobile/Forward+ (Vulkan) | Shader editor → "Inspect Native Shader Code", or capture with RenderDoc and save the SPIR-V. `malioc` accepts SPIR-V (`--vulkan`) |
| Unity | Select shader → Inspector → "Compile and show code" with the GLES3 (or Vulkan) platform; split by variant keywords. Or capture with RenderDoc for Arm. Strip variants first so you analyse the shipped set |
| three.js / WebGL | In the browser: `renderer.debug.onShaderError` hook, or Spector.js / the WebGL Inspector extension to copy the final GLSL ES 3.00 (three injects its chunks + defines). Save as `.frag`/`.vert` |
| Bevy (wgpu) | Convert WGSL with naga: `naga shader.wgsl out.frag --profile es310 --entry-point fragment` (or emit SPIR-V). Shader defs: resolve them as the pipeline does (check the generated code via `WGPU_*` debug/trace options) |
| Any GLES app on device | RenderDoc for Arm GPUs: capture a frame and save the shaders of the expensive draws |

## Running

```bash
python3 scripts/malioc_batch.py --core Mali-G72 dump/ --label-src <project>/shaders --top 15 --json shaders.json
```
Pick `--core` as the reference phone's GPU (`malioc --list`). Run again with a High-tier phone's
GPU (e.g. `Mali-G710`) to see whether a feature is affordable there.

## Reading the output

| Column | Meaning | Act when |
|---|---|---|
| `cycles` (longest path) | cycles per thread on one shader core for the costliest path | top rows on big surfaces (ground, buildings, water, characters) |
| `bound` | pipeline that limits: **A** arithmetic, **LS** load/store (UBO/SSBO/stack), **V** varying interpolation, **T** texture | A: cut math. T: fewer/cheaper samples. V: fewer/16-bit varyings. LS: spilling or big uniform arrays |
| `regs` / `occ` | work registers; occupancy % | > 32 regs on Bifrost → `occ50`: half the threads in flight, latency hidden worse. Simplify, split, reduce live values |
| `SPILL` | stack spilling | always fix: very expensive on mobile |
| `lateZS` | discard/alpha test/depth write or coverage change | breaks early-Z/FPK/LRZ. Avoid on large surfaces, draw such materials last |
| `fp16:N%` | share of arithmetic in 16-bit | low on Bifrost = 2× ALU left on the table: mediump for colour, UV, normals, lighting terms |

Cost of a material ≈ cycles × pixels it covers × passes it appears in (colour + shadow/depth
variants). Combine it with the counter capture: if the GPU is fragment-bound and one material
covers half the screen at 38 cycles/pixel, that's the first target.

## Field example (Godot 4.7 Compatibility, Mali-G72)

| Shader | Cycles/pixel | Bound | Regs / occupancy | fp16 |
|---|---|---|---|---|
| `world.gdshader` (city ground/buildings) | 38.7 | A | 64 / 50% | 0% |
| built-in StandardMaterial3D (Burley diffuse, Kenney models) | 29.6 | A | 64 / 50% | 0% |
| `water.gdshader` | 26.2 | A | 63 / 50% | 0% |
| `person_rig.gdshader` | 21.2 | A | 58 / 50% | 0% |

Arithmetic-bound and register-heavy everywhere. Cheap wins, in order:
1. Gate procedural surface noise per tier, with a uniform or a separate cheap material.
2. Lambert diffuse instead of Burley for built-in materials (`diffuse_mode`).
3. Disable specular where it isn't seen.
4. Move fog and other per-pixel terms to the vertex shader.
5. Use mediump precision where the engine allows it.

## Typical shader fixes on mobile

- Move per-pixel math that varies slowly (fog, rim terms, simple lighting) to the vertex shader
  and interpolate it.
- Use cheaper diffuse/specular models: Lambert, no specular on matte surfaces, Blinn-Phong over
  GGX on Low/Medium.
- Replace procedural noise with a small tiling noise texture lookup (T is often cheaper than A on
  Bifrost), or bake it.
- Branch on a uniform (coherent across the draw), not on per-pixel data, for tier features.
  Better still, compile separate variants.
- Pack textures (ORM). Avoid dependent texture reads in long chains.
