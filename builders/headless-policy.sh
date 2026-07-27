#!/usr/bin/env bash
# THE single headless-policy validator (was three per-builder copies of one regex).
#
#   headless_deps_check  <name> <pkg>...   — fail if any RUNTIME PACKAGE is graphics/audio
#   ldd_closure_check    <name> <elf>...   — fail if any ELF has an unresolved "not found" dep
#
# Sourced by the stack builders (build stage) AND runtime-test.sh (clean-runtime stage), so the
# policy can never drift between the two.

# Mirrors loraham-pi-control lhpc/core/deps.py `_DENY_RE` (the authoritative headless installation
# guard) plus Qt and PipeWire; keep in sync with it. Matched case-insensitively against package
# NAMES (unanchored, so a variant like libgl1-mesa-glx is caught too).
_HEADLESS_DENY='libgtk-|libgdk-|python3-tk|tk[0-9]|libsdl|libx11|libxcb|libxext|libxrandr|libxcursor|libxi[0-9]|libxfixes|libxss|xserver-|xwayland|x11-common|xauth|libwayland-|wayland|libgbm|libdrm|libegl|libgl[0-9x]|mesa-|libllvm|libpulse|libasound|libinput|libxkbcommon|adwaita-|gnome-|kde-|xfce4|lxde|cups|fonts-|libqt|qt[0-9]|qtbase|pipewire|libpipewire|libspa-'

headless_deps_check() {
  local name="$1"; shift
  if printf '%s\n' "$@" | grep -qiE "$_HEADLESS_DENY"; then
    echo "FAIL(${name}): graphics/audio runtime dependency detected — headless policy" >&2
    printf '%s\n' "$@" | grep -iE "$_HEADLESS_DENY" >&2
    return 6
  fi
  return 0
}

ldd_closure_check() {
  local name="$1"; shift
  local bad=0 elf out
  for elf in "$@"; do
    # Static or non-dynamic ELFs report "not a dynamic executable" — that is fine.
    out="$(ldd "$elf" 2>&1 || true)"
    if grep -q "not found" <<<"$out"; then
      echo "FAIL(${name}): unresolved shared-library dependency in ${elf}:" >&2
      grep "not found" <<<"$out" >&2
      bad=1
    fi
  done
  return $bad
}
