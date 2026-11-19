#!/usr/bin/env bash
# Dump the GLSL ES shaders a Godot 4 project really compiles (Compatibility renderer), so the
# Mali Offline Compiler can cost them. Runs the project on the desktop with Mesa in GLES mode and
# Mesa's MESA_SHADER_DUMP_PATH. Needs Mesa (llvmpipe is fine) and xvfb-run if there is no display.
#
#   godot_dump_shaders.sh <godot_project_dir> <out_dir> [seconds=20] [scene res path]
#   malioc_batch.py --core Mali-G72 <out_dir> --label-src <godot_project_dir>/shaders
#
# Visit every area/material you care about during the run (or pass a test scene that shows them):
# Godot compiles shader variants lazily on first use.
set -euo pipefail
proj=${1:?godot project dir}; out=${2:?output dir}; secs=${3:-20}; scene=${4:-}
mkdir -p "$out"
frames=$(( secs * 60 ))
run=(godot --path "$proj" --rendering-driver opengl3_es --render-thread safe --quit-after "$frames")
[ -n "$scene" ] && run+=("$scene")
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || [ -n "${HEADLESS:-}" ]; then
  run=(xvfb-run -a -s "-screen 0 1280x720x24" "${run[@]}")
fi
# A throw-away user data dir: Godot's own shader cache (user://shader_cache) would otherwise skip
# compilation on every run after the first, and the game's saves stay untouched.
tmpdata=$(mktemp -d)
trap 'rm -rf "$tmpdata"' EXIT
# Copy the existing user data (saves, settings) without the shader cache, so the game reaches the
# same scenes as in normal play; the copy is thrown away afterwards.
name=$(sed -n 's/^config\/name="\(.*\)"/\1/p' "$proj/project.godot" | head -1)
src="${XDG_DATA_HOME:-$HOME/.local/share}/godot/app_userdata/$name"
if [ -n "$name" ] && [ -d "$src" ]; then
  mkdir -p "$tmpdata/godot/app_userdata/$name"
  (cd "$src" && find . -path ./shader_cache -prune -o -type f -print0 | cpio -0pdm --quiet "$tmpdata/godot/app_userdata/$name")
  echo "using a copy of $src (without shader_cache)"
fi
# MESA_SHADER_READ_PATH set to a missing dir keeps Mesa from replacing shaders
XDG_DATA_HOME="$tmpdata" MESA_SHADER_CACHE_DISABLE=true MESA_SHADER_DUMP_PATH="$out" MESA_SHADER_READ_PATH=/nonexistent \
  "${run[@]}" >"$out/godot.log" 2>&1 || true
grep -m1 "OpenGL API" "$out/godot.log" || echo "warning: could not confirm the GLES context (see $out/godot.log)"
n=$(ls "$out" | grep -c '\.glsl$' || true)
echo "dumped $n shaders to $out"
[ "$n" -gt 0 ] || { echo "no shaders dumped: is this Mesa? (NVIDIA/AMD proprietary drivers ignore MESA_SHADER_DUMP_PATH)"; exit 1; }
