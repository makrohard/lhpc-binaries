#!/usr/bin/env bash
# Build headless meshtasticd (PlatformIO env `native`) + web assets inside the Trixie container.
# Approach A: lhpc build runs the real recipe, INCLUDING the link-gate build step that proves no
# X11/SDL linkage. The shared headless policy is a second gate on the derived runtime deps.
set -euo pipefail

ROOT=/build/root
DIST=/out
mkdir -p "$ROOT/src" "$DIST"
export LHPC_RUNTIME_ROOT="$ROOT"
source /builders/lib-build.sh
source /builders/headless-policy.sh

read -r M_REMOTE M_PIN M_PATH <<<"$(read_src meshtastic)"
COMMIT="${SOURCE_COMMIT:-$M_PIN}"
echo "meshtastic: $M_REMOTE @ $COMMIT -> $M_PATH"

echo "==> Clone source"
git clone --quiet "$M_REMOTE" "$ROOT/$M_PATH"
git -C "$ROOT/$M_PATH" fetch --quiet origin "$COMMIT" 2>/dev/null || true
git -C "$ROOT/$M_PATH" -c advice.detachedHead=false checkout --quiet "$COMMIT"
HEAD="$(git -C "$ROOT/$M_PATH" rev-parse HEAD)"
[ "$HEAD" = "$COMMIT" ] || { echo "HEAD $HEAD != requested $COMMIT" >&2; exit 4; }

echo "==> lhpc build meshtastic (PlatformIO native; link-gate is a build step — slow)"
if ! "$LHPC" build meshtastic --yes; then
  echo "=== lhpc build log (tail) ==="
  cat "$ROOT"/logs/build-meshtastic*.log 2>/dev/null | tail -100 || true
  exit 5
fi

MTD="$ROOT/build/tools/meshtasticd/meshtasticd"
[ -x "$MTD" ] || { echo "FAIL: meshtasticd not at $MTD" >&2; exit 5; }
file "$MTD"

echo "==> Runtime deps (ldd -> dpkg -S; unowned libraries are a hard failure)"
echo "--- raw ldd ---"; ldd "$MTD" 2>&1 | sed -n '1,40p' || true
mapfile -t DEPS < <(deps_of "$MTD")
require_owned_deps meshtastic "${DEPS[@]:-}"
printf 'runtime_deps: %s\n' "${DEPS[*]:-<none>}"
headless_deps_check meshtastic "${DEPS[@]:-}"
ldd_closure_check meshtastic "$MTD"

run_smoke meshtastic

echo "==> Pack (meshtasticd + web assets + build marker; runtime-root relative)"
STAGE="$(mktemp -d)"
mkdir -p "$STAGE/build/tools/meshtasticd"
cp -a "$ROOT/build/tools/meshtasticd/." "$STAGE/build/tools/meshtasticd/"
MK="$ROOT/$M_PATH/.lhpc-build-complete"
[ -f "$MK" ] && install -D "$MK" "$STAGE/$M_PATH/.lhpc-build-complete" || true
COMPONENTS="{\"meshtastic\": \"$COMMIT\"}"
pack_and_fragment meshtastic "$STAGE" "$COMMIT" "$COMPONENTS" "$SMOKE_MODE" "$SMOKE_RESULT" "${DEPS[@]:-}"
write_provenance meshtastic
