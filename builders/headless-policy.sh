#!/usr/bin/env bash
# THE single headless-policy validator (was three per-builder copies of one regex).
#
#   headless_deps_check  <name> <pkg>...   — fail if any RUNTIME PACKAGE is graphics/audio
#   ldd_closure_check    <name> <elf>...   — fail if any ELF has an unresolved "not found" dep
#
# Sourced by the stack builders (build stage) AND runtime-test.sh (clean-runtime stage), so the
# policy can never drift between the two.

_HEADLESS_DENY='libx11|libsdl|libgtk|mesa|libgl1|wayland|pulse|libxcb'

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
