#!/usr/bin/env bash
# Build the loraham daemon (C++; RadioLib linked in) inside the Trixie container.
# Approach A: drive lhpc's OWN build so the recipe never drifts. Source pins are read
# from lhpc's manifest (no hardcoding); SOURCE_COMMIT overrides the daemon commit only.
set -euo pipefail

ROOT=/build/root
DIST=/out
mkdir -p "$ROOT/src" "$DIST"
export LHPC_RUNTIME_ROOT="$ROOT"
source /builders/lib-build.sh
source /builders/headless-policy.sh

read -r D_REMOTE D_PIN D_PATH  <<<"$(read_src loraham-daemon)"
read -r R_REMOTE R_PIN R_PATH  <<<"$(read_src radiolib)"
# The Meshstick library's pine64 source, where this lhpc declares it: the daemon's build.sh then
# builds lib/libloraham-ch341.so, and the artifact carries it beside the binary.
P_SRC="$(read_src --optional libch341)"
COMMIT="${SOURCE_COMMIT:-$D_PIN}"
echo "daemon   : $D_REMOTE @ $COMMIT -> $D_PATH"
echo "radiolib : $R_REMOTE @ $R_PIN -> $R_PATH"
if [ -n "$P_SRC" ]; then
  read -r P_REMOTE P_PIN P_PATH <<<"$P_SRC"
  echo "libch341 : $P_REMOTE @ $P_PIN -> $P_PATH"
else
  echo "libch341 : not declared by this lhpc (no Meshstick library)"
fi

echo "==> Clone sources"
git clone --quiet "$R_REMOTE" "$ROOT/$R_PATH"
git -C "$ROOT/$R_PATH" -c advice.detachedHead=false checkout --quiet "$R_PIN"
[ -z "$P_SRC" ] || clone_pin "$P_REMOTE" "$P_PIN" "$ROOT/$P_PATH"
git clone --quiet "$D_REMOTE" "$ROOT/$D_PATH"
git -C "$ROOT/$D_PATH" fetch --quiet origin "$COMMIT" 2>/dev/null || true
git -C "$ROOT/$D_PATH" -c advice.detachedHead=false checkout --quiet "$COMMIT"

# Gate: the checked-out commit is exactly what was requested.
HEAD="$(git -C "$ROOT/$D_PATH" rev-parse HEAD)"
[ "$HEAD" = "$COMMIT" ] || { echo "HEAD $HEAD != requested $COMMIT" >&2; exit 4; }

echo "==> lhpc build daemon (real recipe: RadioLib then daemon)"
build_stack daemon || exit 5

BIN="$ROOT/$D_PATH/loraham_daemon/loraham_daemon"
[ -x "$BIN" ] || { echo "FAIL: daemon binary not at $BIN" >&2; exit 5; }
file "$BIN"
ELFS=("$BIN")
if [ -n "$P_SRC" ]; then
  LIB="$(daemon_lib "$BIN" "$P_PIN")"
  file "$LIB"
  ELFS+=("$LIB")
fi

run_smoke daemon

echo "==> Runtime deps (ldd -> dpkg -S; unowned libraries are a hard failure)"
# The binary dlopen()s the library, so ldd of the binary cannot see the library's own deps.
echo "--- raw ldd ---"; for elf in "${ELFS[@]}"; do ldd "$elf" 2>&1 | sed -n '1,25p' || true; done
mapfile -t DEPS < <(deps_of_all "${ELFS[@]}")
require_owned_deps daemon "${DEPS[@]:-}"
printf 'runtime_deps: %s\n' "${DEPS[*]:-<none>}"
headless_deps_check daemon "${DEPS[@]:-}"
ldd_closure_check daemon "${ELFS[@]}"

echo "==> Pack (tar.zst; extracts relative to the runtime root)"
STAGE="$(mktemp -d)"
daemon_stage "$STAGE" "$D_PATH" "$BIN" "${LIB:-}"
pack_and_fragment daemon "$STAGE" "$COMMIT" "$SMOKE_MODE" "$SMOKE_RESULT" "${DEPS[@]:-}"
write_provenance daemon
