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
COMMIT="${SOURCE_COMMIT:-$D_PIN}"
echo "daemon   : $D_REMOTE @ $COMMIT -> $D_PATH"
echo "radiolib : $R_REMOTE @ $R_PIN -> $R_PATH"

echo "==> Clone sources"
git clone --quiet "$R_REMOTE" "$ROOT/$R_PATH"
git -C "$ROOT/$R_PATH" -c advice.detachedHead=false checkout --quiet "$R_PIN"
git clone --quiet "$D_REMOTE" "$ROOT/$D_PATH"
git -C "$ROOT/$D_PATH" fetch --quiet origin "$COMMIT" 2>/dev/null || true
git -C "$ROOT/$D_PATH" -c advice.detachedHead=false checkout --quiet "$COMMIT"

# Gate: the checked-out commit is exactly what was requested.
HEAD="$(git -C "$ROOT/$D_PATH" rev-parse HEAD)"
[ "$HEAD" = "$COMMIT" ] || { echo "HEAD $HEAD != requested $COMMIT" >&2; exit 4; }

echo "==> lhpc build daemon (real recipe: RadioLib then daemon)"
"$LHPC" build daemon --yes

BIN="$ROOT/$D_PATH/loraham_daemon/loraham_daemon"
[ -x "$BIN" ] || { echo "FAIL: daemon binary not at $BIN" >&2; exit 5; }
file "$BIN"

run_smoke daemon

echo "==> Runtime deps (ldd -> dpkg -S; unowned libraries are a hard failure)"
echo "--- raw ldd ---"; ldd "$BIN" 2>&1 | sed -n '1,25p' || true
mapfile -t DEPS < <(deps_of "$BIN")
require_owned_deps daemon "${DEPS[@]:-}"
printf 'runtime_deps: %s\n' "${DEPS[*]:-<none>}"
headless_deps_check daemon "${DEPS[@]:-}"
ldd_closure_check daemon "$BIN"

echo "==> Pack (tar.zst; extracts relative to the runtime root)"
STAGE="$(mktemp -d)"
install -D "$BIN" "$STAGE/$D_PATH/loraham_daemon/loraham_daemon"
pack_and_fragment daemon "$STAGE" "$COMMIT" "$SMOKE_MODE" "$SMOKE_RESULT" "${DEPS[@]:-}"
write_provenance daemon
