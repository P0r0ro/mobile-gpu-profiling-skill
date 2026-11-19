#!/usr/bin/env python3
"""Hardware-counter GPU profiling for Android games (Arm Mali via Arm Performance Studio).

  gpu_profile.py detect                                  # device, GPU, driver, which path to use
  gpu_profile.py mali-capture -p com.x.game -s 25         # config + headless Streamline capture + analysis
  gpu_profile.py mali-analyze run/capture.apc --fps 60    # re-analyse an existing capture
  gpu_profile.py gpu-busy -p com.x.game -s 30             # sysfs GPU busy/clock sampler (any vendor, best effort)

Writes <out>/report.md and <out>/metrics.json: GPU utilisation, fragment vs geometry load,
fragments per screen pixel, shader ALU cost, texture compression/mipmaps, Z-kill, culling,
bandwidth per frame — each with a verdict and the fix it points to.

Needs: adb, Python 3.9+, Arm Performance Studio (found via --arm-ps, $ARM_PS_DIR,
~/.local/opt/Arm_Performance_Studio_*, /opt/arm*). The app must be a debuggable build.
"""
from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import zipfile

CACHE = os.path.expanduser("~/.cache/mobile-gpu-profiling")

# ----------------------------------------------------------------------------- tools


def find_adb(explicit: str | None) -> str:
    for c in (explicit, os.environ.get("ADB"), os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
              os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"), shutil.which("adb")):
        if c and os.path.exists(c):
            return c
    sys.exit("adb not found: pass --adb or set $ADB")


def find_arm_ps(explicit: str | None) -> str:
    cands = [explicit, os.environ.get("ARM_PS_DIR")]
    cands += sorted(glob.glob(os.path.expanduser("~/.local/opt/Arm_Performance_Studio_*")), reverse=True)
    cands += sorted(glob.glob("/opt/arm*/Arm_Performance_Studio_*"), reverse=True)
    cands += sorted(glob.glob(os.path.expanduser("~/Arm_Performance_Studio_*")), reverse=True)
    for c in cands:
        if c and os.path.exists(os.path.join(c, "streamline", "Streamline-cli")):
            return c
    sys.exit("Arm Performance Studio not found: extract it (e.g. ~/.local/opt/) or pass --arm-ps / set $ARM_PS_DIR")


class Dev:
    def __init__(self, adb: str, serial: str | None):
        self.adb = adb
        if not serial:
            out = subprocess.run([adb, "devices"], capture_output=True, text=True).stdout
            devs = [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip().endswith("device")]
            if not devs:
                sys.exit("no adb device (adb devices is empty; for Wi-Fi: adb connect <ip>:5555)")
            serial = devs[0]
        self.serial = serial
        self.env = dict(os.environ, PATH=os.path.dirname(adb) + os.pathsep + os.environ.get("PATH", ""))

    def sh(self, cmd: str, timeout: float = 20) -> str:
        try:
            return subprocess.run([self.adb, "-s", self.serial, "shell", cmd], capture_output=True,
                                  text=True, timeout=timeout, errors="replace").stdout
        except subprocess.TimeoutExpired:
            return ""

    def run(self, *a: str, timeout: float = 120) -> str:
        return subprocess.run([self.adb, "-s", self.serial, *a], capture_output=True, text=True,
                              timeout=timeout, errors="replace").stdout


def clean_gatord(d: Dev, pkg: str) -> None:
    """Kill every gatord, including agents left in the app's sandbox by a crashed capture
    (`run-as <pkg> pkill` misses them on some builds; they block the next capture's sockets)."""
    d.sh("pkill gatord")
    for ln in d.sh("ps -A -o USER,PID,NAME").splitlines():
        p = ln.split()
        if len(p) == 3 and p[2] == "gatord":
            if p[0] == "shell":
                d.sh(f"kill -9 {p[1]}")
            else:
                d.sh(f"run-as {pkg} kill -9 {p[1]}")


def gpu_info(d: Dev) -> dict:
    gles = next((ln for ln in d.sh("dumpsys SurfaceFlinger").splitlines() if "GLES:" in ln), "")
    m = re.search(r"GLES:\s*([^,]+),\s*([^,]+),\s*(.*)", gles)
    size = d.sh("wm size")
    ov = re.search(r"Override size:\s*(\d+)x(\d+)", size) or re.search(r"Physical size:\s*(\d+)x(\d+)", size)
    w, h = (int(ov.group(1)), int(ov.group(2))) if ov else (0, 0)
    return {"vendor": m.group(1).strip() if m else "", "gpu": m.group(2).strip() if m else "",
            "driver": m.group(3).strip() if m else "",
            "model": d.sh("getprop ro.product.model").strip(),
            "android": d.sh("getprop ro.build.version.release").strip(),
            "sdk": d.sh("getprop ro.build.version.sdk").strip(),
            "screen": [max(w, h), min(w, h)]}


# ----------------------------------------------------------------------------- fps sampler


class FpsSampler(threading.Thread):
    """Samples SurfaceFlinger present timestamps for the app's layer while a capture runs."""

    def __init__(self, d: Dev, pkg: str, delay: float):
        super().__init__(daemon=True)
        self.d, self.pkg, self.delay = d, pkg, delay
        self.ts: set[int] = set()
        self.stop = threading.Event()

    def layer(self) -> str | None:
        lines = [ln.strip() for ln in self.d.sh("dumpsys SurfaceFlinger --list").splitlines() if self.pkg in ln]
        for pref in ("SurfaceView", "BLAST", ""):
            for ln in lines:
                if pref in ln and "Background" not in ln:
                    return ln
        return None

    def run(self) -> None:
        time.sleep(self.delay)
        lay = None
        while not self.stop.is_set():
            lay = lay or self.layer()
            if lay:
                for ln in self.d.sh(f"dumpsys SurfaceFlinger --latency '{lay}'").splitlines()[1:]:
                    p = ln.split()
                    if len(p) == 3 and p[1].isdigit() and 0 < int(p[1]) < 2**62:
                        self.ts.add(int(p[1]))
            self.stop.wait(1.0)

    def fps(self) -> float | None:
        t = sorted(self.ts)
        if len(t) < 30:
            return None
        gaps = [(b - a) / 1e9 for a, b in zip(t, t[1:])]
        # drop gaps between sampling windows (> 0.25 s) so coverage holes don't count as slow frames
        good = [g for g in gaps if g < 0.25]
        return len(good) / sum(good) if good else None


# ----------------------------------------------------------------------------- Mali: config


def template_for(arm: str, gpu: str) -> str | None:
    jar = next(iter(glob.glob(os.path.join(arm, "streamline", "plugins", "com.arm.streamline_*.jar"))), None)
    if not jar:
        return None
    with zipfile.ZipFile(jar) as z:
        names = [n.split("/")[-1][:-3] for n in z.namelist() if "templates/stock/" in n and n.endswith(".st")]
    g = gpu.replace("Mali ", "Mali-").strip()
    for n in names:  # "Mali-G72 - Bifrost"
        if n.split(" - ")[0].strip().lower() == g.lower():
            return n
    return None


def mali_config(d: Dev, arm: str, pkg: str, gpu: str, cpu: bool, force: bool) -> str:
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{re.sub(r'[^A-Za-z0-9_-]', '_', gpu)}-{'cpu' if cpu else 'gpu'}.xml")
    if os.path.exists(path) and not force:
        return path
    tpl = template_for(arm, gpu)
    if not tpl:
        sys.exit(f"no Streamline template for '{gpu}'. Export a configuration.xml from the Streamline GUI "
                 f"(Counter Configuration > Save) and pass --config")
    sl = os.path.join(arm, "streamline")
    gatord = os.path.join(sl, "bin", "android", "arm64", "gatord")
    print(f"# generating counter config from template '{tpl}' (one-off, cached in {CACHE})")
    d.run("push", gatord, "/data/local/tmp/gatord")
    d.sh("chmod 0777 /data/local/tmp/gatord")
    clean_gatord(d, pkg)
    proc = subprocess.Popen([d.adb, "-s", d.serial, "shell", "/data/local/tmp/gatord", "--android-pkg", pkg],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.time()
    while time.time() - t0 < 20:
        line = proc.stdout.readline() if proc.stdout else ""
        if "Gator ready" in line:
            break
    raw = path[:-4] + "-full.xml"
    out = subprocess.run([os.path.join(sl, "Streamline-cli"), "-generate-config", "-template", tpl,
                          "-include-default", "-o", raw, f"adb:{d.serial}"],
                         capture_output=True, text=True, env=d.env)
    clean_gatord(d, pkg)
    proc.kill()
    if not os.path.exists(raw):
        sys.exit("config generation failed:\n" + out.stdout[-1500:] + out.stderr[-500:])
    lines = open(raw).read().split("\n")
    keep = [ln for ln in lines if 'counter="' not in ln or re.search(r'counter="ARM_Mali-', ln)
            or (cpu and re.search(r'counter="ARMv[0-9]', ln))]
    # Mali timeline needs DDK r47+; harmless to drop for counter analysis
    keep = [ln for ln in keep if "MaliTimeline_Perfetto" not in ln]
    open(path, "w").write("\n".join(keep))
    print(f"# config: {sum('counter=' in k for k in keep)} counters -> {path}")
    return path


# ----------------------------------------------------------------------------- Mali: capture


def mali_capture(a: argparse.Namespace) -> int:
    d = Dev(find_adb(a.adb), a.serial)
    arm = find_arm_ps(a.arm_ps)
    info = gpu_info(d)
    if info["vendor"] != "ARM":
        sys.exit(f"GPU is {info['vendor']} {info['gpu']}: Streamline GPU counters are Mali-only. "
                 "See references/adreno.md for Qualcomm devices.")
    print(f"# {info['model']} Android {info['android']} {info['gpu']} driver {info['driver'][:40]}")
    if info["sdk"].isdigit() and int(info["sdk"]) < 30:
        print("# note: Arm supports Android 11+; on older versions counters work but frame markers "
              "(Performance Advisor per-frame report) may be missing")
    cfg = a.config or mali_config(d, arm, a.package, info["gpu"], a.cpu, a.regen_config)
    out = os.path.abspath(a.out or f"gpu-profile-{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out, exist_ok=True)
    apc = os.path.join(out, "capture.apc")
    sl = os.path.join(arm, "streamline")
    d.sh(f"am force-stop {a.package}")
    clean_gatord(d, a.package)
    time.sleep(3)  # let a force-stop / earlier gatord settle, or the new capture sees the app die and stops
    cmd = [sys.executable, os.path.join(sl, "bin", "android", "streamline_me.py"), "--device", d.serial,
           "--package", a.package, "--config", cfg, "--daemon", os.path.join(sl, "bin", "android", "arm64", "gatord"),
           "--headless", apc, "--headless-timeout", str(int(a.seconds)), "--lwi-mode", "counters",
           "--gpu-timeline", "no", "--overwrite"]
    if a.activity:
        cmd += ["--package-activity", a.activity]
    sampler = FpsSampler(d, a.package, delay=a.warmup)
    sampler.start()
    print(f"# capturing {a.seconds:.0f} s (the app is launched by the script; play the scene you want to measure)")
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=d.env)
    sampler.stop.set()
    open(os.path.join(out, "streamline_me.log"), "w").write(r.stdout + r.stderr)
    if not os.path.isdir(apc):
        print(r.stdout[-2000:], r.stderr[-1000:])
        print("capture failed. If the log mentions the device pull or the child exiting with status 1, try "
              "without --cpu (CPU counters are blocked on many Samsung/Android<11 kernels).")
        return 2
    fps = a.fps or sampler.fps()
    json.dump({"device": info, "fps_measured": fps, "seconds": a.seconds, "package": a.package},
              open(os.path.join(out, "session.json"), "w"), indent=1)
    return analyze(apc, arm, out, fps, info["screen"], a.target_fps, info)


# ----------------------------------------------------------------------------- Mali: analysis


def load_timeline(arm: str, apc: str) -> tuple[list[str], list[list[float]]]:
    cli = os.path.join(arm, "streamline", "Streamline-cli")
    r = subprocess.run([cli, "-report", "-timeline", "-default-template", "with-metrics", apc],
                       capture_output=True, text=True, cwd=os.path.dirname(apc) or ".")
    txt = r.stdout
    i = txt.find("Index (s),")
    if i < 0:
        sys.exit("no timeline in report output:\n" + txt[-1500:] + r.stderr[-800:])
    rows = list(csv.reader(io.StringIO(txt[i:])))
    head = rows[0]
    data = []
    for row in rows[1:]:
        if len(row) != len(head):
            continue
        try:
            data.append([float(v) if v else 0.0 for v in row])
        except ValueError:
            continue
    return head, data


def constants(apc: str) -> dict:
    c = {"cores": 0, "bus_bits": 128, "tile": 16}
    log = os.path.join(apc, "gator-log.txt")
    if os.path.exists(log):
        t = open(log, errors="replace").read()
        for k, rx in (("cores", r"num_shader_cores=(\d+)"), ("bus_bits", r"axi_bus_width=(\d+)"), ("tile", r"tile_size=(\d+)")):
            m = re.search(rx, t)
            if m:
                c[k] = int(m.group(1))
    return c


def analyze(apc: str, arm: str, out: str, fps: float | None, screen: list[int], target: float, info: dict | None) -> int:
    head, data = load_timeline(arm, apc)
    if not data:
        sys.exit("empty timeline")
    k = constants(apc)
    sums = [0.0] * len(head)
    for r in data:
        for i, v in enumerate(r):
            sums[i] += v
    S = dict(zip(head, sums))
    dur = data[-1][0] - data[0][0] + (data[1][0] - data[0][0] if len(data) > 1 else 0.001)

    def g(*names: str) -> float:
        for n in names:  # exact column or "Category:Name" suffix match, first hit
            for col in head:
                if col == n or col.endswith(":" + n) or col == "Mali " + n:
                    return S[col]
        return 0.0

    def ratio(a: float, b: float) -> float | None:
        return a / b if b else None

    fcol = next((i for i, c in enumerate(head) if c.startswith("Mali Clock:Frequency")), None)
    acol = next((i for i, c in enumerate(head) if c.endswith("GPU Cycles:GPU active")), None)
    freq = statistics.fmean(r[fcol] for r in data if r[fcol] > 0) if fcol is not None else 0.0
    active = g("GPU Cycles:GPU active")
    # utilisation row by row: the clock column is 0 in idle/unsampled rows, and DVFS changes it
    util_num = util_den = 0.0
    if fcol is not None and acol is not None:
        for j, r in enumerate(data):
            dt = (data[j + 1][0] - r[0]) if j + 1 < len(data) else (r[0] - data[j - 1][0] if j else 0.001)
            if r[fcol] > 0:
                util_num += r[acol]
                util_den += r[fcol] * dt
    cores = k["cores"] or 1
    bus_bytes = k["bus_bits"] / 8
    frag_q = g("GPU Cycles:Fragment queue active")
    nonfrag_q = g("GPU Cycles:Non-fragment queue active")
    tiler = g("GPU Cycles:Tiler active")
    core_active = g("Shader Core Cycles:Programmable core active", "Shader Core Cycles:Execution core active")
    frag_active = g("Shader Core Cycles:Fragment active")
    quads = g("Fragment Quads:Rasterized fine quads", "Fragment Quads:Rasterized quads")
    tiles = g("Fragment Tiles:Tiles")
    ezs_t, ezs_k = g("Fragment ZS Quads:Early ZS tested quads"), g("Fragment ZS Quads:Early ZS killed quads")
    lzs_t, lzs_k = g("Fragment ZS Quads:Late ZS tested quads"), g("Fragment ZS Quads:Late ZS killed quads")
    tex = g("Texture Unit Requests:Texture requests")
    tris = g("Input Primitives:Triangle primitives")
    alu = g("ALU Instructions:Executed instructions")
    warps = g("Shader Warps:Fragment warps")
    rd = g("External Bus Beats:Read beats") * bus_bytes
    wr = g("External Bus Beats:Write beats") * bus_bytes
    v16, v32 = g("Varying Unit Requests:16-bit interpolation slots"), g("Varying Unit Requests:32-bit interpolation slots")

    m: dict = {
        "duration_s": round(dur, 2), "gpu_clock_mhz": round(freq / 1e6), "shader_cores": cores,
        "fps": round(fps, 1) if fps else None,
        "gpu_util": ratio(util_num, util_den),
        "fragment_queue_util": ratio(frag_q, active), "non_fragment_queue_util": ratio(nonfrag_q, active),
        "tiler_util": ratio(tiler, active),
        "shader_core_util": ratio(core_active, active * cores), "fragment_core_util": ratio(frag_active, active * cores),
        "read_gbps": rd / dur / 1e9, "write_gbps": wr / dur / 1e9,
        "fragments_per_s": quads * 4 / dur, "tiles_per_s": tiles / dur,
        "early_zs_kill_rate": ratio(ezs_k, ezs_t), "late_zs_test_share": ratio(lzs_t, quads),
        "late_zs_kill_share": ratio(lzs_k, quads),
        "fpk_occluding_share": ratio(g("Fragment FPK HSR Quads:Occluding quads"), quads),
        "unchanged_tiles_killed": ratio(g("Fragment Tiles:Killed unchanged tiles"), tiles),
        "tex_compressed_share": ratio(g("Texture Unit Requests:Compressed texture requests"), tex),
        "tex_mipmapped_share": ratio(g("Texture Unit Requests:Mipmapped texture requests"), tex),
        "tex_trilinear_share": ratio(g("Texture Unit Requests:Trilinear filtered requests"), tex),
        "tex_requests_per_fragment": ratio(tex, quads * 4),
        "alu_instr_per_fragment": ratio(alu, warps),  # executed instructions per warp = per fragment thread
        "triangles_per_s": tris / dur,
        "facing_culled_share": ratio(g("Primitive Culling:Facing or XY plane culled primitives"), tris),
        "visible_share": ratio(g("Primitive Culling:Visible primitives"), tris),
        "fp16_varying_share": ratio(v16, v16 + v32),
        "issue_starvation": ratio(g("Shader Core Stall Cycles:Instruction issue starvation"), core_active),
        "bus_read_stall_share": ratio(g("External Bus Stall Cycles:Read stalls"), active),
        "fragment_jobs_per_s": g("GPU Jobs:Fragment jobs") / dur,
    }
    if fps:
        px = screen[0] * screen[1] if screen and screen[0] else 0
        m["fragments_per_screen_pixel"] = ratio(m["fragments_per_s"] / fps, px) if px else None
        m["render_passes_per_frame"] = m["fragment_jobs_per_s"] / fps
        m["triangles_per_frame"] = m["triangles_per_s"] / fps
        m["bandwidth_mb_per_frame"] = (rd + wr) / dur / fps / 1e6
        m["gpu_ms_per_frame"] = (m["gpu_util"] or 0) * 1000.0 / fps
    pa = run_pa(arm, apc, out)
    findings = interpret(m, target)
    write_report(out, m, findings, info, screen, pa)
    return 0


def run_pa(arm: str, apc: str, out: str) -> str:
    cli = os.path.join(arm, "streamline", "Streamline-cli")
    d = os.path.join(out, "performance_advisor")
    r = subprocess.run([cli, "-pa", "--no-progress", f"--directory={d}", "--type=html,json", apc],
                       capture_output=True, text=True)
    if "Cannot find any frames" in r.stdout + r.stderr:
        return "no frame markers in capture (Android < 11, or the LWI layer did not attach); counter analysis below is per-second x measured fps"
    html = glob.glob(os.path.join(d, "*.html"))
    return f"Performance Advisor report: {html[0]}" if html else "Performance Advisor produced no report: " + (r.stdout + r.stderr)[-300:]


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def interpret(m: dict, target: float) -> list[tuple[str, str, str]]:
    """(level, finding, fix) — ordered by importance."""
    f: list[tuple[str, str, str]] = []
    gu, fq, nq = m.get("gpu_util") or 0, m.get("fragment_queue_util") or 0, m.get("non_fragment_queue_util") or 0
    fps = m.get("fps")
    if gu >= 0.85:
        if fq >= max(nq, 0.5):
            f.append(("BOUND", f"GPU {pct(gu)} busy and fragment-bound (fragment queue {pct(fq)}, cores {pct(m.get('fragment_core_util'))} in fragment work)",
                      "cost is per pixel: cut overdraw/extra passes, shader ALU, texture fetches; vertex/draw reductions will not raise fps"))
        else:
            f.append(("BOUND", f"GPU {pct(gu)} busy and geometry-bound (non-fragment {pct(nq)}, tiler {pct(m.get('tiler_util'))})",
                      "cut triangles in view (LOD, visibility ranges, culling), vertex shader cost, shadow caster count"))
    elif gu < 0.6 and fps and fps < target * 0.95:
        f.append(("BOUND", f"GPU only {pct(gu)} busy while fps {fps:.0f} < {target:.0f}", "CPU-bound (game logic, draw submission, animation, physics): profile the CPU, reduce draw calls"))
    elif fps and fps >= target * 0.95:
        f.append(("OK", f"fps at target with GPU {pct(gu)} busy", "headroom = 1 - util; keep ~25% for thermals before adding High-tier effects"))
    fpp = m.get("fragments_per_screen_pixel")
    if fpp is not None:
        lvl = "WARN" if fpp > 2.5 else "INFO"
        f.append((lvl, f"{fpp:.1f} fragments rasterised per screen pixel per frame (all passes) over {m.get('render_passes_per_frame', 0):.1f} fragment passes/frame",
                  "includes shadow-map and off-screen passes: shrink/skip shadow maps (resolution, distance, casters), avoid full-screen passes, reduce transparent overdraw (aim < 2.0 on mid phones)"))
    alu = m.get("alu_instr_per_fragment")
    if alu is not None and alu > 30:
        f.append(("WARN", f"~{alu:.0f} ALU instructions per fragment", "heavy fragment shaders: run malioc_batch.py on the game's shaders, move work to the vertex shader, use mediump, drop specular/fog per-pixel math on Low/Medium"))
    tc = m.get("tex_compressed_share")
    if tc is not None and tc < 0.5:
        f.append(("WARN", f"only {pct(tc)} of texture samples are GPU-compressed (mipmapped {pct(m.get('tex_mipmapped_share'))})",
                  "import 3D textures as VRAM-compressed (ASTC/ETC2) with mipmaps; note shadow-map / render-target samples are never compressed, so check which textures dominate"))
    lz = m.get("late_zs_test_share")
    if lz is not None and lz > 0.1:
        f.append(("WARN", f"{pct(lz)} of quads use late ZS (discard/alpha test/depth write in shader)", "alpha-clip and discard defeat early-Z/FPK: use opaque geometry or tight meshes, draw alpha-tested last"))
    ez = m.get("early_zs_kill_rate")
    if ez is not None and ez < 0.2 and (fpp or 0) > 2:
        f.append(("INFO", f"early-ZS kills only {pct(ez)} of tested quads", "draw opaque front-to-back (engines sort by distance; big ground/sky planes last), or add a cheap depth pre-pass only if overdraw is high"))
    fc = m.get("facing_culled_share")
    if fc is not None and fc < 0.3 and (m.get("triangles_per_s") or 0) > 1e6:
        f.append(("INFO", f"only {pct(fc)} of triangles back-face culled", "materials with cull disabled (two-sided) double vertex/tiler cost: enable back-face culling where meshes are closed"))
    f16 = m.get("fp16_varying_share")
    if f16 is not None and f16 < 0.3:
        f.append(("INFO", f"{pct(f16)} of varyings are 16-bit", "mediump varyings (UVs, colours, normals) halve interpolation cost/bandwidth where the engine lets you set precision"))
    bw = m.get("bandwidth_mb_per_frame")
    if bw is not None and bw > 40:
        f.append(("WARN", f"{bw:.0f} MB external memory traffic per frame ({m['read_gbps'] + m['write_gbps']:.1f} GB/s)", "bandwidth drives heat: fewer/lower-res render targets, compressed textures, MSAA resolve on-tile, no post passes"))
    st = m.get("issue_starvation")
    if st is not None and st > 0.2:
        f.append(("INFO", f"instruction issue starvation {pct(st)}", "cores wait on memory/texture latency: smaller/compressed textures, fewer dependent texture reads"))
    return f


def write_report(out: str, m: dict, findings: list, info: dict | None, screen: list[int], pa: str) -> None:
    json.dump({"metrics": m, "findings": findings, "device": info}, open(os.path.join(out, "metrics.json"), "w"), indent=1)
    L = ["# GPU profile (Mali hardware counters)", ""]
    if info:
        L.append(f"Device: {info.get('model')} · Android {info.get('android')} · {info.get('gpu')} · driver {info.get('driver', '')[:40]} · screen {screen[0]}x{screen[1]}")
    L.append(f"Capture {m['duration_s']} s · GPU clock ~{m['gpu_clock_mhz']} MHz · {m['shader_cores']} shader cores · fps {m['fps'] or 'unknown (pass --fps)'}")
    L += ["", "## Findings", ""]
    for lvl, what, fix in findings:
        L.append(f"- **{lvl}** {what}\n  - → {fix}")
    L += ["", "## Metrics", "", "| metric | value |", "|---|---|"]
    for k_, v in m.items():
        if isinstance(v, float):
            v = f"{100 * v:.1f}%" if (k_.endswith(("_util", "_share", "_rate", "_killed", "starvation"))) else f"{v:,.2f}"
        L.append(f"| {k_} | {v} |")
    L += ["", f"Performance Advisor: {pa}", ""]
    open(os.path.join(out, "report.md"), "w").write("\n".join(L))
    print("\n".join(L))
    print(f"\n# written: {out}/report.md, metrics.json")


def mali_analyze(a: argparse.Namespace) -> int:
    arm = find_arm_ps(a.arm_ps)
    apc = os.path.abspath(a.capture)
    out = os.path.abspath(a.out or os.path.dirname(apc))
    sess = os.path.join(os.path.dirname(apc), "session.json")
    info, fps = None, a.fps
    screen = [int(x) for x in a.screen.lower().split("x")] if a.screen else [0, 0]
    if os.path.exists(sess):
        s = json.load(open(sess))
        info = s.get("device")
        fps = fps or s.get("fps_measured")
        if not a.screen and info:
            screen = info.get("screen", screen)
    return analyze(apc, arm, out, fps, screen, a.target_fps, info)


# ----------------------------------------------------------------------------- generic


def detect(a: argparse.Namespace) -> int:
    d = Dev(find_adb(a.adb), a.serial)
    info = gpu_info(d)
    print(json.dumps(info, indent=1))
    v = info["vendor"].upper()
    if v == "ARM":
        print("-> Mali: use `mali-capture` (Arm Performance Studio / Streamline). Shader cost: malioc_batch.py")
    elif "QUALCOMM" in v:
        print("-> Adreno: Snapdragon Profiler (Realtime/Trace) or Android GPU Inspector; see references/adreno.md. "
              "`gpu-busy` gives a quick utilisation read without tools")
    else:
        print("-> other GPU (PowerVR/Xclipse/...): use the vendor tool or Android GPU Inspector; `gpu-busy` may work")
    return 0


GPU_NODES = [  # (busy, clock) candidates; many need root on retail Samsung builds
    ("/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage", "/sys/class/kgsl/kgsl-3d0/devfreq/cur_freq"),
    ("/sys/kernel/gpu/gpu_busy", "/sys/kernel/gpu/gpu_clock"),
    ("/sys/class/misc/mali0/device/utilization", "/sys/class/misc/mali0/device/clock"),
]


def gpu_busy(a: argparse.Namespace) -> int:
    d = Dev(find_adb(a.adb), a.serial)
    node = None
    for busy, clk in GPU_NODES:
        v = d.sh(f"cat {busy} 2>&1").strip()
        if v and "denied" not in v and "No such" not in v:
            node = (busy, clk)
            break
    if not node:
        print("no readable GPU busy node (needs root on this build). Use the vendor profiler.")
        return 1
    print(f"# {node[0]}")
    vals = []
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        raw = d.sh(f"cat {node[0]}; cat {node[1]} 2>/dev/null").split()
        nums = [int(x) for x in re.findall(r"\d+", " ".join(raw))]
        if nums:
            busy = nums[0]
            vals.append(busy)
            print(f"{time.time() - t0:6.1f}s busy {busy}%  clock {nums[-1] if len(nums) > 1 else '-'}")
        time.sleep(1)
    if vals:
        print(f"# median busy {statistics.median(vals)}%  max {max(vals)}%")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--adb")
    ap.add_argument("--serial")
    ap.add_argument("--arm-ps", help="Arm Performance Studio install dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect")
    c = sub.add_parser("mali-capture")
    c.add_argument("-p", "--package", required=True)
    c.add_argument("-s", "--seconds", type=float, default=25)
    c.add_argument("--activity")
    c.add_argument("--warmup", type=float, default=8, help="seconds before fps sampling starts (app launch)")
    c.add_argument("--cpu", action="store_true", help="also capture CPU PMU counters (fails on some Samsung/Android<11)")
    c.add_argument("--config", help="use this configuration.xml instead of generating one")
    c.add_argument("--regen-config", action="store_true")
    c.add_argument("--fps", type=float, help="override measured fps")
    c.add_argument("--target-fps", type=float, default=60)
    c.add_argument("-o", "--out")
    an = sub.add_parser("mali-analyze")
    an.add_argument("capture")
    an.add_argument("--fps", type=float)
    an.add_argument("--screen", help="WxH, e.g. 2220x1080")
    an.add_argument("--target-fps", type=float, default=60)
    an.add_argument("-o", "--out")
    b = sub.add_parser("gpu-busy")
    b.add_argument("-p", "--package")
    b.add_argument("-s", "--seconds", type=float, default=30)
    a = ap.parse_args()
    return {"detect": detect, "mali-capture": mali_capture, "mali-analyze": mali_analyze, "gpu-busy": gpu_busy}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
