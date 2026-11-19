# Mali counters: meaning, formulas, thresholds, fixes

Streamline's timeline CSV has one row per sample (~1 ms). Shader-core counters are **summed over
all cores**, so divide by the core count (`num_shader_cores` in the capture's `gator-log.txt`;
G72 MP18 = 18). Bus beats are `axi_bus_width/8` bytes each (128-bit = 16 B). Column names below
are `Mali <Category>:<Counter>` as Bifrost (G71/G72/G76/G52) exports them. Valhall and 5th-gen
use similar categories with some renames (e.g. "Execution core active"). `gpu_profile.py` tries
the alternatives.

## Utilisation: is the GPU the limit?

| Metric | Formula | Read |
|---|---|---|
| GPU utilisation | Σ `GPU Cycles:GPU active` / Σ (`Clock:Frequency` × dt), over rows with a non-zero clock | > 85%: GPU-bound. < 60% while fps is low: CPU-bound. Compute it **row by row**: DVFS changes the clock, and idle rows report 0 Hz |
| Fragment queue share | `Fragment queue active` / `GPU active` | ~90%+ with a high GPU util: pixel work is the limit |
| Non-fragment queue share | `Non-fragment queue active` / `GPU active` | vertex shading, compute, tiling |
| Tiler share | `Tiler active` / `GPU active` | primitive setup/binning; high with many small triangles |
| Shader core utilisation | `Programmable core active` / (`GPU active` × cores) | confirms the shader cores are the busy part |
| GPU ms per frame | util × 1000 / fps | the GPU's real cost per frame at the current clock; compare it with the frame budget |

Field data (Galaxy Note 9, Godot 4.7 Compatibility, 2220×1080, 60 fps city scene):
- GPU util 94%, fragment queue 91%, non-fragment 18%, tiler 12%.
- About 15.9 ms of GPU per 16.6 ms frame: no thermal headroom.
- 7.1 fragment jobs/frame, 3.9 fragments per screen pixel.
- About 53 ALU instructions per fragment, 0% compressed texture samples.
- About 40 MB of memory traffic per frame.

So: fragment-bound. Cutting draw calls would not have helped the GPU.

## Fragment work

| Metric | Formula | Healthy | Fix when bad |
|---|---|---|---|
| Fragments per screen pixel (all passes) | `Rasterized fine quads` × 4 / fps / (screen W×H) | < 2 | shadow-map size/distance/casters; transparent overdraw; extra passes (blits, copies, post) |
| Fragment passes per frame | `GPU Jobs:Fragment jobs` / fps | 2–4 (main + shadow + UI) | each extra pass = a framebuffer load/store. Find it in Frame Advisor |
| ALU instructions per fragment | `ALU Instructions:Executed instructions` / `Shader Warps:Fragment warps` | < 20–30 (Bifrost) | malioc the top shaders; move math to the vertex shader; mediump; per-tier shader features |
| Early ZS kill rate | `Early ZS killed quads` / `Early ZS tested quads` | — (scene dependent) | low with high overdraw: opaque front-to-back, big planes last |
| Late ZS share | `Late ZS tested quads` / rasterized quads | < 10% | discard/alpha-test/depth writes in shaders: use opaque geometry, tight alpha cards |
| FPK occluding share | `FPK HSR Quads:Occluding quads` / rasterized quads | high | Forward Pixel Kill removes hidden opaque quads; discard and blending disable it |
| Unchanged tiles killed | `Killed unchanged tiles` / `Tiles` | — | transaction elimination; high on static UI. Free bandwidth savings |
| Partial warps | `Partial fragment warps` / `Fragment warps` | low | many tiny triangles (sub-quad) waste lanes: LOD the micro-detail |

## Textures

| Metric | Formula | Healthy | Fix |
|---|---|---|---|
| Compressed share | `Compressed texture requests` / `Texture requests` | most scene samples | VRAM-compressed import (ASTC/ETC2). Shadow-map and render-target samples are never compressed, so a shadow-heavy scene reads low anyway |
| Mipmapped share | `Mipmapped texture requests` / `Texture requests` | most scene samples | enable mipmaps on 3D textures (no mips = cache thrash and shimmer at distance) |
| Trilinear share | `Trilinear filtered requests` / `Texture requests` | low on Low/Medium | bilinear+mips on mobile; trilinear/anisotropic only on High |
| Texture requests per fragment | `Texture requests` / fragments | 1–4 | many dependent fetches: pack textures (ORM), drop layers on low tiers |
| Issue starvation | `Instruction issue starvation` / core active | < 15% | cores waiting on memory: compressed/smaller textures, fewer dependent reads |

## Geometry

| Metric | Formula | Fix |
|---|---|---|
| Triangles per frame | `Input Primitives:Triangle primitives` / fps | LOD, visibility ranges; shadow passes re-submit casters |
| Facing-culled share | `Facing or XY plane culled` / triangles | ~40–50% for closed meshes. Low = two-sided materials (cull disabled) |
| Visible share | `Visible primitives` / triangles | low with high counts: submitting lots of off-screen/back geometry |
| Varying 16-bit share | `16-bit interpolation slots` / all slots | mediump varyings for UV/colour/normal |
| Position/varying shading stalls | Tiler stall cycles | IDVS: keep position computation cheap and separate |

## Memory bandwidth (heat)

| Metric | Formula | Healthy (2018–2020 phones) |
|---|---|---|
| Read GB/s, write GB/s | `External Bus Beats:Read/Write beats` × bus bytes / s | sustained total < ~2–3 GB/s for a game that must not throttle |
| MB per frame | (read + write) / fps | < ~30–40 MB at 60 fps |
| Bus read stalls | `External Bus Stall Cycles:Read stalls` / GPU active | > 5–10% means memory-latency bound |

Big writers: shadow maps, intermediate colour buffers (HDR, render scaling, post), MSAA resolved
to memory instead of on-tile, and uncompressed texture uploads.

## Architecture notes

- **Bifrost** (G31/G51/G52/G71/G72/G76): warp width 4 (G71/G72) or 8 (G76/G52); a 16×16 tile.
  Prefers mediump. Register-heavy shaders (> 32 regs) halve occupancy.
- **Valhall** (G57/G68/G77/G78/G310/G510/G610/G710/G715): 16-wide warps. A better FMA unit and
  better tolerance of fp32, but fp16 still pays.
- **5th gen** (G720/G725/Immortalis-G720/G925): deferred vertex shading (DVS) changes tiler
  economics; the Mali timeline is available with recent DDKs.
- Templates for each GPU live inside the Streamline jar (`"<GPU> - <arch>.st"`), and the script
  picks them by the GPU name SurfaceFlinger reports.
