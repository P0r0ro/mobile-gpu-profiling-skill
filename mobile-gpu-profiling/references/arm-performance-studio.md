# Arm Performance Studio (2026.x)

A free download from developer.arm.com (Performance Studio). Linux: a `.tgz`. Extract it anywhere,
for example `~/.local/opt/Arm_Performance_Studio_<ver>/`. The scripts find it there, or via
`$ARM_PS_DIR` or `--arm-ps`.

| Component | Path | What it is for |
|---|---|---|
| Streamline | `streamline/Streamline` (GUI), `streamline/Streamline-cli` | CPU/GPU counter timeline. Headless capture plus CSV export (`-report`) |
| Performance Advisor | `Streamline-cli -pa` | HTML/JSON report per frame: fps/frame-time chart, bound per region, draw/primitive budgets. Needs frame markers |
| Frame Advisor | `frame_advisor/FrameAdvisor-gui` | Captures one or more frames: render-pass graph, per-draw and per-pass cost, redundant passes, bandwidth per attachment, mesh/vertex efficiency |
| RenderDoc for Arm GPUs | `renderdoc_for_arm_gpus/bin/qrenderdoc` | API-level frame debugger with Arm extensions (state, textures, shaders) |
| Mali Offline Compiler | `mali_offline_compiler/malioc` | Static shader cost per GPU (`malioc --list`). Works without a device |
| Hub | `performance-studio-cli.sh` | Launcher GUI |

Licence: Arm EULA in `license_terms/`. Don't redistribute the binaries in your repo; document
the download instead.

## Device requirements

- Android 11+ is officially supported. Android 10 worked for GPU counters on a Galaxy Note 9
  (Mali-G72, driver r19p0), but frame markers never arrived.
- A debuggable app (`run-as` must work). No root is needed.
- The Mali timeline (`MaliTimeline_Perfetto`) needs Mali DDK r47+. Leave it off on older
  drivers.
- Many Samsung/Android 10 kernels block CPU PMU counters for this flow. Use GPU-only counters
  (the script's default; `--cpu` opts in).

## Headless pipeline (what `gpu_profile.py mali-capture` automates)

1. **Counter config** (one-off per GPU):
   ```bash
   adb push streamline/bin/android/arm64/gatord /data/local/tmp/ && adb shell chmod 0777 /data/local/tmp/gatord
   adb shell /data/local/tmp/gatord --android-pkg <pkg> &          # daemon mode, prints "Gator ready"
   streamline/Streamline-cli -generate-config -template "Mali-G72 - Bifrost" -include-default \
       -o mali_g72.xml adb:<serial-or-ip:port>
   ```
   - Template names are `"<GPU> - <arch>"`. They're listed inside
     `streamline/plugins/com.arm.streamline_*.jar` under `resources/templates/stock/`.
   - Remove CPU counter lines (`ARMv8_*`) unless you need them, and remove
     `MaliTimeline_Perfetto` on old drivers.
   - `-help` shows `-generate-config <template> <target>`, but the working form is
     `-generate-config -template <name> [-o file] <target>`. The output file name must end in
     `.xml`.
2. **Capture**:
   ```bash
   python3 streamline/bin/android/streamline_me.py --device <serial> --package <pkg> \
       --config mali_g72.xml --daemon streamline/bin/android/arm64/gatord \
       --headless out.apc --headless-timeout 30 --lwi-mode counters --gpu-timeline no --overwrite
   ```
   - The script installs `gatord` and the LWI layers, launches the main activity, captures,
     pulls the `.apc` directory and disables the debug layers again.
   - Before capturing, kill stale `gatord` agents left in the app sandbox by an earlier crash
     (`adb shell ps -A | grep gatord`, then `run-as <pkg> kill -9 <pid>`). Otherwise the capture
     stops early.
   - Without `--headless` it only prepares the device and waits for the GUI to connect.
3. **Export counters**: `Streamline-cli -report -timeline -default-template with-metrics out.apc`
   writes CSV **to stdout** (one row per ~1 ms sample, columns `Mali <Category>:<Counter>`).
   `gpu_profile.py` parses it.
4. **Performance Advisor**: `Streamline-cli -pa --directory=pa --type=html,json out.apc`.
   - Useful options: `--main-thread`, `--process`, `--clip-start/--clip-end`, `--regions=file.csv`
     for named regions, `--build-name`/`--device-name` for report headers, and `--diff-report` to
     compare two JSON reports (before/after a fix).
   - "Cannot find any frames" means no frame boundaries were captured. Supply them with
     `--lwi-mode counters` (Android 11+), or with app annotations (the gator annotation API, as
     in `streamline/gator/annotate`).

## Frame Advisor (single-frame deep dive)

Use it when the counters say fragment- or bandwidth-bound and you need to know *which pass* or
*which draw*:
1. Launch `FrameAdvisor-gui`, connect the device (adb), pick the debuggable package, and capture
   1–3 frames in the scene of interest.
2. Read:
   - the **Render graph** (passes, attachments, which passes load/store, redundant clears and
     resolves);
   - **per-pass bandwidth** (shadow maps, intermediate buffers);
   - **draw call list** with primitives and shading cost;
   - **mesh efficiency** (vertex reuse, index/vertex ratio).
3. Typical finds:
   - an extra full-screen copy/blit pass;
   - a shadow map larger than needed;
   - attachments stored to memory that are never read (should be discarded);
   - the same mesh drawn in several passes.

It's GUI-driven. Agents should ask the user to run the capture, or read exported reports.

## RenderDoc for Arm GPUs

An API frame debugger: inspect the state, bound textures and shader source for a draw, check
texture formats (is it really ASTC? are mips present?), and view overdraw in the texture viewer.
Use it to answer "why is this texture uncompressed / this draw two-sided / this pass there at
all".

## Mali Offline Compiler

```bash
malioc --list                                   # supported GPUs
malioc --core Mali-G72 --fragment shader.frag   # text report
malioc --core Mali-G72 --format json --fragment shader.frag
```
GLSL ES (`#version 300 es`), Vulkan GLSL or SPIR-V. See `shader-cost.md` for getting engine
shaders out and reading the result. `malioc_batch.py` ranks whole directories.
