---
name: mobile-gpu-profiling
description: >
  Reads what the phone's GPU is actually doing from its hardware counters, so fixes target the real
  limit. Arm Mali via Arm Performance Studio (Streamline headless capture, Performance Advisor, Mali
  Offline Compiler, Frame Advisor, RenderDoc for Arm) and Qualcomm Adreno via Snapdragon Profiler /
  Android GPU Inspector. Ships scripts that capture Mali counters over adb and turn them into
  verdicts (fragment- vs geometry- vs CPU-bound, fragments per pixel, shader ALU per pixel, texture
  compression, early-Z, bandwidth per frame), rank an engine's real compiled shaders by Mali cycles
  (Godot shaders dumped through Mesa), and sample GPU busy on any vendor. Use when the cheap checks
  (`mobile-3d-rendering`) say the GPU is the limit but draw/triangle cuts don't help, to decide if a
  High-tier effect fits, to find the expensive shader, or for GPU-side oddities on Mali/Adreno
  phones. Not for first-pass fps/memory/thermal triage (`mobile-3d-rendering`) or CPU profiling
  (`performance-optimization`).
---

# Mobile GPU profiling (Mali and Adreno hardware counters)

`mobile-3d-rendering` measures from the outside: fps, memory, temperature and engine counters.
This skill opens the GPU and reads its hardware counters. It answers: is the GPU really the limit,
and if so, is it **pixels, vertices, textures, bandwidth or shader math**? Each needs a different
fix, so guessing wastes days.

**When to bring it out** (escalation, not the first step):
- `mobile-3d-rendering` triage shows fps below target with draw counts in budget, or fps doesn't
  move when draws and triangles are cut;
- deciding whether a High-tier effect (normal maps, MSAA 4x, bloom halo, more lights) fits the
  budget on the reference phone;
- finding which shader or material is expensive;
- heat with no obvious cause (bandwidth shows up here);
- GPU-side oddities on one vendor.

Always finish by re-measuring with `mobile-3d-rendering/scripts/device_probe.py` on the device
(fps, p95, memory slope, thermals). Counters explain; the probe confirms.

## 0. Detect the GPU and pick the path

```bash
S=~/.claude/skills/mobile-gpu-profiling/scripts
python3 $S/gpu_profile.py detect          # vendor, GPU, driver, Android version, screen size
```

| GPU | Counter capture | Shader cost | Frame capture |
|---|---|---|---|
| **Arm Mali / Immortalis** | `gpu_profile.py mali-capture` (Streamline, headless) | `malioc_batch.py` (Mali Offline Compiler) | Frame Advisor, RenderDoc for Arm GPUs |
| **Qualcomm Adreno** | Snapdragon Profiler Realtime/Trace (GUI), or AGI on Android 11+ supported devices | Adreno offline compiler (if available) or SDP shader stats | Snapdragon Profiler Snapshot, AGI frame profiler |
| other (PowerVR, Xclipse) | vendor tool / AGI; `gpu_profile.py gpu-busy` | — | RenderDoc / AGI |

Install notes for each tool: `references/arm-performance-studio.md`, `references/adreno.md`.
The app must be a **debuggable build** (Godot: debug export; Unity: Development Build;
`android:debuggable="true"`).

## 1. Mali: capture and read the counters

```bash
python3 $S/gpu_profile.py mali-capture -p <package> -s 30 -o gpu-profile-<name>
#   first run builds a counter config from Arm's template for that GPU (cached in ~/.cache/mobile-gpu-profiling)
#   the script launches the app; play the scene you want to measure (worst case: crowds, combat, night)
python3 $S/gpu_profile.py mali-analyze gpu-profile-<name>/capture.apc --fps 60   # re-read later
```

It writes `report.md` + `metrics.json`. Read the verdict at the top, then the numbers:

| Metric | Healthy on a mid phone | If not |
|---|---|---|
| `gpu_util` | < 75–80% at target fps (thermal headroom) | > 85%: GPU-bound, read the next rows. < 60% with low fps: CPU-bound, so profile the CPU |
| `fragment_queue_util` vs `non_fragment_queue_util` / `tiler_util` | — | whichever is near 100% is the limit: **fragment** = per-pixel cost; **non-fragment/tiler** = vertices, triangles, shadow casters |
| `fragments_per_screen_pixel` | < 2.0 (all passes) | overdraw, shadow maps, extra render passes (see `render_passes_per_frame`) |
| `alu_instr_per_fragment` | < 20–30 on Bifrost | heavy fragment shaders → §2 |
| `tex_compressed_share` / `tex_mipmapped_share` | high for scene textures | uncompressed/no-mip textures waste bandwidth and cache (shadow maps and render targets are never compressed, so check which textures dominate) |
| `late_zs_test_share` | < 10% | discard/alpha test, or shader depth writes, disable early-Z |
| `early_zs_kill_rate`, `fpk_occluding_share` | high is good | sort opaque front-to-back; avoid discard on big surfaces |
| `bandwidth_mb_per_frame` | < ~30–40 MB at 60 fps on 2018–2020 phones | render targets, uncompressed textures, MSAA resolves → heat |
| `facing_culled_share` | ~40–50% for closed meshes | two-sided materials doubling vertex work |
| `fp16_varying_share` | high | mediump varyings halve interpolation cost |

The full counter-to-fix reference, with formulas, is in `references/mali-counters.md`.

**Frame markers**: Performance Advisor's per-frame HTML report needs frame boundaries from Arm's
layer. Arm supports Android 11+. On Android 10 (tested on a Galaxy Note 9) the counters work but
the layer's frames never reached `gatord`, so the script falls back to per-second counters × the
fps it measures from SurfaceFlinger during the capture.

## 2. Find the expensive shader (any engine, Mali cost model)

```bash
# Godot (Compatibility/GLES3): dump the GLSL ES the game really compiles, via Mesa on the desktop
HEADLESS=1 $S/godot_dump_shaders.sh <godot_project> dump/ 20 [res://test_scene.tscn]
python3 $S/malioc_batch.py --core Mali-G72 dump/ --label-src <godot_project>/shaders --top 15
```

- The ranking lists cycles per pixel (or per vertex) and which pipeline bounds it (A arithmetic,
  LS load/store, V varying, T texture). Labels name *your* shader file; "engine" means a built-in
  material.
- Flags: `occ50` means more than 32 work registers, so half the threads are in flight (latency
  hidden worse); `SPILL` means stack spilling; `lateZS` means discard; `fp16:0%` means no 16-bit
  math.
- Cost of a shader ≈ cycles × pixels it covers. A 38-cycle shader on the ground and buildings
  matters more than a 60-cycle one on a small sign.
- Unity, three.js and Bevy shader extraction: `references/shader-cost.md`.

The dump script runs the game against a throw-away copy of its user data, so saves and settings
are untouched and Godot's shader cache doesn't hide compiles. Visit the areas you care about
during the run (or pass a test scene). Godot compiles variants lazily.

## 3. Adreno (Qualcomm)

Use Snapdragon Profiler: Realtime for live metrics, Trace for a timeline, Snapshot for one frame
with per-draw stats. Read the same questions from its metrics: `% Shaders Busy`, `% Time Shading
Fragments` vs `Vertices`, `Avg Bytes / Fragment`, `% Texture Fetch Stall`, `% Texture L1 Miss`,
`ALU / Fragment`, `% Prims Trivially Rejected`, `Read/Write Total`. The metric-to-fix table is in
`references/adreno.md`.

On Android 11+ devices AGI supports, Android GPU Inspector gives vendor-neutral counters plus a
frame profiler. For a quick utilisation read without tools, `gpu_profile.py gpu-busy -s 30` reads
the kernel's busy node where the build allows it.

## 4. From counters to changes (then verify)

1. Fix the **largest** limiter the report names. Make one change at a time.
2. Typical mappings:

   | Limit | Fixes |
   |---|---|
   | fragment-bound | fewer/smaller shadow maps and shorter shadow distance, less transparent overdraw, cheaper shaders on big surfaces (move math to the vertex shader, drop per-pixel noise/fog/specular on Low/Medium, mediump), compressed+mipmapped textures, lower internal resolution only where it is proven safe (Mali-G72 leaked memory with render scaling) |
   | geometry/tiler-bound | LOD and visibility ranges, fewer shadow casters, back-face culling on, simpler skinning on crowds |
   | bandwidth/heat | compressed textures, fewer render targets/passes, MSAA instead of post AA, no full-screen post |
   | CPU-bound (GPU < 60% busy) | draw calls, scripts, animation, physics (`performance-optimization`) |

3. Re-capture the same scene and compare `metrics.json` before/after (GPU ms per frame, the
   limiter's share).
4. Confirm on the device with `device_probe.py` for at least 60 s (fps, p95, memory slope,
   thermals). A counter win that heats the phone or leaks is not a win.
5. Record the finding in the project notes (what was bound, what fixed it, before/after numbers).

## Hard-won rules (from real captures)

- Generate the counter config from the GPU's template, and keep **GPU counters only** by
  default. CPU PMU counters made the capture child exit at start on a Samsung Android 10 kernel.
- Wi-Fi adb works, but the device id is `ip:port`. Pass it everywhere (the scripts do).
- A crashed capture can leave `gatord` agents alive inside the app's sandbox. They block the next
  capture's sockets ("Address already in use") and cut it short. The script kills them by PID
  (`run-as <pkg> pkill` did not).
- Force-stopping the app right before a capture can end it after a second. The script waits for
  things to settle.
- `Streamline-cli -generate-config` needs `-template <name>` *before* the target, despite its
  `-help` text.
- Godot keeps a GLES shader cache in `user://shader_cache`, so a second desktop run compiles
  nothing. Use a fresh `XDG_DATA_HOME` (the dump script does).
- Running the real game on the desktop for audits writes its autosave. Isolate the user data
  directory.

## References

| File | Read when |
|---|---|
| `references/arm-performance-studio.md` | installing/using Streamline, Performance Advisor, Frame Advisor, RenderDoc for Arm, malioc; headless pipeline details |
| `references/mali-counters.md` | what each Mali metric means, formulas, thresholds, Bifrost vs Valhall vs 5th gen |
| `references/adreno.md` | Snapdragon Profiler / AGI setup and metric-to-fix table for Adreno |
| `references/shader-cost.md` | getting compiled shaders out of Godot/Unity/three.js/Bevy and reading malioc output |
