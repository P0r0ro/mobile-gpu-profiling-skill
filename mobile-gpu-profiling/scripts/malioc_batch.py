#!/usr/bin/env python3
"""Rank shaders by cost on a Mali GPU with the Mali Offline Compiler (malioc).

  malioc_batch.py --core Mali-G72 DUMP_DIR_OR_FILES... [--label-src godot/shaders] [--top 15] [--json out.json]

Inputs: GLSL ES files (#version 300/310/320 es) or SPIR-V. The stage is taken from the file name
(FS_*/VS_* as dumped by Mesa, *.frag/*.vert, *_fs/*_vs) or forced with --stage.
--label-src: directory of the engine's own shader sources (.gdshader/.shader/.glsl/.hlsl); each
dumped shader is labelled with the source file whose identifiers it contains most, so you can see
which of *your* materials is expensive (engine built-ins are labelled "engine").

Cycles are per thread (fragment = per pixel sample, vertex = per vertex) on ONE shader core:
  cost of a draw ~ cycles x fragments shaded / (cores x clock).
Flags: stack spilling, low occupancy (>32 work registers on Bifrost), late ZS / discard,
fp16 share low, texture-bound or arithmetic-bound.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

PIPE_SHORT = {"arithmetic": "A", "load_store": "LS", "varying": "V", "texture": "T"}


def find_malioc(explicit: str | None) -> str:
    cands = [explicit, os.environ.get("MALIOC"), shutil.which("malioc")]
    cands += sorted(glob.glob(os.path.expanduser("~/.local/opt/Arm_Performance_Studio_*/mali_offline_compiler/malioc")), reverse=True)
    cands += sorted(glob.glob("/opt/arm*/Arm_Performance_Studio_*/mali_offline_compiler/malioc"), reverse=True)
    for c in cands:
        if c and os.path.exists(c):
            return c
    sys.exit("malioc not found: pass --malioc or set $MALIOC")


def stage_of(path: str, forced: str | None) -> str | None:
    if forced:
        return forced
    b = os.path.basename(path).lower()
    if b.startswith("fs_") or b.endswith((".frag", "_fs.glsl", ".fs")):
        return "fragment"
    if b.startswith("vs_") or b.endswith((".vert", "_vs.glsl", ".vs")):
        return "vertex"
    if b.startswith("cs_") or b.endswith(".comp"):
        return "compute"
    return None


def ident_sets(src_dir: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for p in glob.glob(os.path.join(src_dir, "**", "*"), recursive=True):
        if not p.endswith((".gdshader", ".shader", ".glsl", ".hlsl", ".frag", ".vert", ".gdshaderinc")):
            continue
        t = open(p, errors="replace").read()
        ids = set(re.findall(r"\b(?:uniform|varying|global uniform|instance uniform)\s+(?:\w+\s+)?\w+\s+(\w+)", t))
        ids |= set(re.findall(r"^\s*\w+\s+(\w+)\s*\([^;{]*\)\s*\{", t, re.M))  # user functions
        ids -= {"vertex", "fragment", "light", "main", "start", "process"}
        if ids:
            out[os.path.relpath(p, src_dir)] = ids
    return out


def label(text: str, srcs: dict[str, set[str]]) -> str:
    best, score = "engine", 0
    for name, ids in srcs.items():
        # Godot prefixes user identifiers with m_ in generated GLSL; other engines keep the name
        s = sum(1 for i in ids if re.search(r"\b(?:m_)?" + re.escape(i) + r"\b", text))
        if s > score and s >= min(2, len(ids)):
            best, score = name, s
    return best


def defines(text: str) -> set[str]:
    return set(re.findall(r"^#define\s+([A-Z][A-Z0-9_]+)", text, re.M))


def variant_tags(text: str, common: set[str]) -> str:
    """Defines that distinguish this variant (shadow pass, skinning, instancing, ...)."""
    keep = sorted(d for d in defines(text) - common
                  if any(k in d for k in ("SHADOW", "DEPTH", "MODE", "SKIN", "INSTANC", "LIGHT", "FOG", "ALPHA", "MULTIVIEW", "LIGHTMAP", "USE_")))
    return ",".join(keep)[:70]


def run_one(malioc: str, core: str, path: str, stage: str) -> dict:
    r = subprocess.run([malioc, "--core", core, "--format", "json", f"--{stage}", path],
                       capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
        sh = d["shaders"][0]
    except (json.JSONDecodeError, KeyError, IndexError):
        return {"file": path, "stage": stage, "error": (r.stdout + r.stderr).strip()[-300:]}
    props = {p["name"]: p["value"] for p in sh.get("properties", [])}
    v = next((x for x in sh.get("variants", []) if x.get("name") == "Main"), (sh.get("variants") or [{}])[0])
    vprops = {p["name"]: p["value"] for p in v.get("properties", [])}
    perf = v.get("performance", {})
    pipes = perf.get("pipelines", [])
    lp = perf.get("longest_path_cycles", {})
    cyc = dict(zip(pipes, lp.get("cycle_count", [])))
    return {"file": path, "stage": stage, "variants": len(sh.get("variants", [])),
            "cycles": {PIPE_SHORT.get(k, k): round(c or 0, 2) for k, c in cyc.items()},
            "longest": round(max([c or 0 for c in cyc.values()] or [0]), 2),
            "bound": "+".join(PIPE_SHORT.get(b, b) for b in lp.get("bound_pipelines", []) if b),
            "work_registers": vprops.get("work_registers_used"), "occupancy": vprops.get("thread_occupancy"),
            "spill": vprops.get("has_stack_spilling"), "fp16": vprops.get("fp16_arithmetic"),
            "late_zs": props.get("uses_late_zs_test") or props.get("uses_late_zs_update"),
            "modifies_coverage": props.get("modifies_coverage"), "side_effects": props.get("has_side_effects"),
            "notes": sh.get("notes", []) + sh.get("warnings", [])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--core", default="Mali-G72", help="malioc --list shows supported GPUs")
    ap.add_argument("--stage", choices=["fragment", "vertex", "compute"])
    ap.add_argument("--label-src", help="engine shader source dir, to name dumped shaders")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json")
    ap.add_argument("--malioc")
    a = ap.parse_args()
    malioc = find_malioc(a.malioc)
    files: list[str] = []
    for i in a.inputs:
        files += sorted(glob.glob(os.path.join(i, "*"))) if os.path.isdir(i) else [i]
    jobs = [(f, stage_of(f, a.stage)) for f in files]
    jobs = [(f, s) for f, s in jobs if s]
    if not jobs:
        sys.exit("no shaders with a recognisable stage (name them *.frag/*.vert or use --stage)")
    srcs = ident_sets(a.label_src) if a.label_src else {}
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        res = list(ex.map(lambda j: run_one(malioc, a.core, j[0], j[1]), jobs))
    texts = {r["file"]: open(r["file"], errors="replace").read() for r in res if "error" not in r}
    common: dict[str, set[str]] = {}
    for st in ("fragment", "vertex", "compute"):
        ds = [defines(texts[r["file"]]) for r in res if "error" not in r and r["stage"] == st]
        common[st] = set.intersection(*ds) if len(ds) > 1 else set()
    for r in res:
        if "error" not in r:
            t = texts[r["file"]]
            r["label"] = label(t, srcs) if srcs else ""
            r["tags"] = variant_tags(t, common[r["stage"]])
    ok = [r for r in res if "error" not in r]
    bad = [r for r in res if "error" in r]
    print(f"# {len(ok)} shaders compiled for {a.core} ({len(bad)} failed)")
    for stage in ("fragment", "vertex", "compute"):
        rows = sorted((r for r in ok if r["stage"] == stage), key=lambda r: -r["longest"])
        if not rows:
            continue
        print(f"\n## {stage}: top {min(a.top, len(rows))} of {len(rows)} by longest-path cycles")
        print(f"{'cycles':>6} {'bound':>5} {'A':>5} {'LS':>5} {'V':>5} {'T':>5} {'regs':>4} {'occ':>4} {'flags':14} {'label':28} tags / file")
        for r in rows[:a.top]:
            c = r["cycles"]
            fl = []
            if r["spill"]:
                fl.append("SPILL")
            if r["occupancy"] is not None and r["occupancy"] < 100:
                fl.append(f"occ{r['occupancy']}")
            if r["late_zs"] or r["modifies_coverage"]:
                fl.append("lateZS")
            if r["fp16"] is not None and r["fp16"] < 30:
                fl.append(f"fp16:{r['fp16']}%")
            print(f"{r['longest']:6.2f} {r['bound']:>5} {c.get('A', 0):5.2f} {c.get('LS', 0):5.2f} {c.get('V', 0):5.2f} {c.get('T', 0):5.2f} "
                  f"{r['work_registers'] or 0:4d} {r['occupancy'] or 0:4d} {' '.join(fl):14} {r.get('label', '')[:28]:28} "
                  f"{r.get('tags', '')} {os.path.basename(r['file'])[:24]}")
    if bad:
        print(f"\n# failed to compile ({len(bad)}): first error: {bad[0]['error'][:200]}")
    if a.json:
        json.dump(res, open(a.json, "w"), indent=1)
    print("\nRead: cycles per thread on one core; 'bound' = the pipeline that limits it "
          "(A arithmetic, LS load/store, V varying, T texture). Fix the top rows that run on the most pixels first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
