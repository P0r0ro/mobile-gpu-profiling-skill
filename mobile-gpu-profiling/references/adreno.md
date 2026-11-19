# Adreno (Qualcomm Snapdragon)

> Status: this path is documented, not yet tested on a device. Neither the Snapdragon Profiler
> download (it needs a Qualcomm login) nor an Adreno phone was available when the skill was
> written. When you first use it, check the metric names against your Snapdragon Profiler
> version and update this file.

## Tools

| Tool | Get it | Use |
|---|---|---|
| **Snapdragon Profiler (SDP)** | qualcomm.com/developer/software/snapdragon-profiler (Qualcomm ID login; also via Qualcomm Package Manager). Windows/macOS/Linux | Realtime metrics, Trace (timeline incl. GPU stages), Snapshot (one frame: per-draw metrics, shaders, textures) |
| **Android GPU Inspector (AGI)** | gpuinspector.dev (open source) | System profiler with GPU counters (Perfetto) + frame profiler. Android 11+ and only devices with the GPU counter producer (many Adreno 6xx/7xx, Pixel) |
| **Adreno offline shader compiler** | part of some Adreno GPU SDK releases *(check availability)* | static shader stats like malioc |
| kernel busy node | `/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage`, `.../devfreq/cur_freq` | `gpu_profile.py gpu-busy`. Often readable without root on Snapdragon builds |

### Snapdragon Profiler on Linux

1. Download the Linux tarball while logged in.
2. Install the dependencies listed in the docs (historically Mono plus `libc++1`/`libc++abi1`; on
   Fedora, `mono-complete` and `libcxx`).
3. Extract it, e.g. to `~/.local/opt/SnapdragonProfiler/`, and run its launcher script.
4. Connect with adb. Use the same `adb` binary as the host (SDP may ship its own; point it at the
   SDK's adb if both exist).
5. The app must be debuggable for per-app API traces (Snapshot). Realtime system metrics work for
   any app.

## Metric → meaning → fix

Read the same questions as on Mali: is the GPU the limit, and is it pixels, vertices, textures,
bandwidth or ALU? Metric names are as SDP shows them in Realtime/Trace (GPU General, GPU Stalls,
GPU Shader Processing, GPU Texture, GPU Memory Stats, GPU Primitive Processing).

| SDP metric | Means | Healthy | Fix when bad |
|---|---|---|---|
| `% GPU Busy` / GPU Utilization | GPU occupancy | < 75–80% at target fps | > 85%: GPU-bound, read below. Low with low fps: CPU-bound |
| `% Shaders Busy` | shader processors working | — | high with fragments dominating: pixel cost |
| `% Time Shading Fragments` vs `% Time Shading Vertices` | where shader time goes | fragments usually dominate | vertices high: LOD, fewer casters, cheaper vertex shaders, skinning on fewer bones |
| `% Time ALUs Working`, `ALU / Fragment`, `Fragment ALU Instructions (Full/Half)` | arithmetic load and precision | more Half than Full | mediump; move math to vertex; cheaper lighting on low tiers |
| `% Texture Pipes Busy`, `Textures / Fragment` | texture load | — | fewer layers, ORM packing |
| `% Texture L1 Miss`, `% Texture L2 Miss`, `% Texture Fetch Stall` | texture cache behaviour | low | compressed (ASTC/ETC2) + mipmapped textures, smaller atlases |
| `% Linear/Nearest/Anisotropic Filtered` | filtering mix | aniso only on High | bilinear + mips on Low/Medium |
| `Avg Bytes / Fragment`, `Avg Bytes / Vertex` | bandwidth per item | low | compressed textures, fewer render targets, slimmer vertex formats |
| `Read Total (Bytes/sec)`, `Write Total (Bytes/sec)` | memory bandwidth | ~2–3 GB/s sustained for no throttling | shadow-map size, intermediate buffers, post passes |
| `% Stalled on System Memory` | memory-latency bound | low | bandwidth fixes above |
| `% Vertex Fetch Stall` | vertex buffer fetch | low | interleaved compact vertex formats, index reuse, fewer attributes |
| `Pre-clipped Polygons/Second`, `% Prims Trivially Rejected`, `% Prims Clipped` | geometry throughput and culling | high rejection = sending off-screen geometry | CPU culling, visibility ranges, LOD |
| `Fragments Shaded / Second` ÷ fps ÷ (W×H) | fragments per pixel (overdraw + passes) | < 2 | as Mali: overdraw, shadow maps, extra passes |
| LRZ metrics (`LRZ: Pixels killed` …) | Adreno low-res Z early rejection | high kill | opaque front-to-back. Discard/alpha test and depth writes from shaders disable LRZ |

### Adreno-specific behaviour

- **FlexRender**: Adreno switches between binning (tiled) and direct rendering per pass. Many small
  passes or mid-frame render-target switches force extra resolves. Keep passes few, and clear or
  discard attachments.
- **LRZ** (low-resolution Z) is Adreno's early-reject. It's lost for the rest of a pass after a
  draw with discard, depth writes from the shader, or some blend states. Draw such objects last.
- Adreno 5xx Vulkan drivers are older and buggier. GLES is often the safer API on them.
- The kernel's `gpu_busy_percentage` is a cheap utilisation signal for an adaptive quality
  governor on Snapdragon devices.

## AGI quick use

1. Install AGI. The device needs Android 11+ and must be on AGI's supported list; the app must be
   debuggable.
2. System Profile → select GPU counters → capture 10–20 s.
3. Read GPU utilisation, fragment vs vertex shading time, texture stalls and bandwidth in the
   Perfetto UI.
4. Frame Profile (Vulkan, and GLES on newer AGI versions) → per-render-pass and per-draw GPU time.
