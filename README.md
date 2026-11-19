# mobile-gpu-profiling: read the phone GPU's hardware counters

A [Claude Code](https://claude.com/claude-code) skill (and standalone scripts) for finding out
**what the GPU is actually doing** in an Android 3D game:
- is it the limit at all, and is the cost in pixels, vertices, textures, bandwidth or shader math;
- which shader is expensive.

It covers **Arm Mali** (via Arm Performance Studio) and **Qualcomm Adreno** (via Snapdragon
Profiler / Android GPU Inspector). It's the deep-dive companion to
[mobile-3d-rendering](https://github.com/P0r0ro/mobile-3d-rendering-skill): that skill does the
cheap triage (fps, memory, thermals, draw counts) and the fixes; this one explains the GPU side
when triage says the GPU is the limit.

## What it found on a real game (Galaxy Note 9, Mali-G72, Godot 4.7 Compatibility)

| | |
|---|---|
| GPU busy | **94%**, 15.9 ms of a 16.6 ms frame (no thermal headroom) |
| Limit | **fragment** (pixel) work 91%; vertex/tiler 12–18%, so cutting draw calls wouldn't raise fps |
| Fragments per screen pixel | 3.9 over 7.1 fragment passes per frame |
| Shader math | ~53 ALU instructions per pixel; the city shader is 38.7 cycles/pixel, 64 registers, 50% occupancy, 0% fp16 |
| Textures | 0% of samples GPU-compressed, 14% mipmapped |
| Bandwidth | ~40 MB per frame |

## Contents

```
mobile-gpu-profiling/
├── SKILL.md                          # when to escalate, the Mali/Adreno paths, counters → fixes, hard-won rules
├── references/
│   ├── arm-performance-studio.md     # Streamline headless pipeline, Performance Advisor, Frame Advisor, RenderDoc, malioc
│   ├── mali-counters.md              # every metric: formula, healthy range, fix; Bifrost/Valhall/5th gen notes
│   ├── adreno.md                     # Snapdragon Profiler / AGI setup and metric → fix table
│   └── shader-cost.md                # getting compiled shaders out of Godot/Unity/three.js/Bevy; reading malioc
└── scripts/
    ├── gpu_profile.py                # detect · mali-capture · mali-analyze · gpu-busy
    ├── malioc_batch.py               # rank shaders by Mali cycles, label them by your source files
    └── godot_dump_shaders.sh         # dump the GLSL ES a Godot game really compiles (via Mesa)
```

## Usage

```bash
S=mobile-gpu-profiling/scripts
python3 $S/gpu_profile.py detect                                   # GPU, driver, Android version → which path
python3 $S/gpu_profile.py mali-capture -p com.example.game -s 30   # capture + report.md / metrics.json
HEADLESS=1 $S/godot_dump_shaders.sh path/to/godot_project dump 20  # Godot only
python3 $S/malioc_batch.py --core Mali-G72 dump --label-src path/to/godot_project/shaders
```

`mali-capture` does the whole pipeline:
1. Generates a counter configuration from Arm's template for the detected GPU (cached).
2. Kills stale `gatord` agents.
3. Runs a headless Streamline capture through `streamline_me.py`, while sampling the app's real
   fps from SurfaceFlinger.
4. Exports the counter timeline and computes the metrics.
5. Writes a verdict with fixes, and runs Performance Advisor when frame markers are present.

## Requirements

- adb and Python 3.9+. A **debuggable** build of the app.
- **Mali**: [Arm Performance Studio](https://developer.arm.com/Tools%20and%20Software/Arm%20Performance%20Studio)
  (free; extract to `~/.local/opt/` or set `$ARM_PS_DIR`).
  - Arm supports Android 11+. On Android 10 the counters still work, but there's no per-frame
    Performance Advisor report.
- **Adreno**: [Snapdragon Profiler](https://www.qualcomm.com/developer/software/snapdragon-profiler)
  (needs a Qualcomm ID) or [Android GPU Inspector](https://gpuinspector.dev). The Adreno
  path is documented but not yet device-tested; corrections are welcome.
- Godot shader dump: Mesa (llvmpipe is fine) and `xvfb-run` on headless machines.

The Arm and Qualcomm tools are not included. Download them from the vendors under their licences.

## Install as a Claude Code skill

```bash
git clone https://github.com/P0r0ro/mobile-gpu-profiling-skill.git
ln -s "$PWD/mobile-gpu-profiling-skill/mobile-gpu-profiling" ~/.claude/skills/mobile-gpu-profiling
```
Restart Claude Code. Use `/mobile-gpu-profiling`, or ask things like "is the GPU fragment-bound?"
or "which shader is the most expensive on the Note 9?".

## License

[MIT](LICENSE) for this skill's text and scripts.
