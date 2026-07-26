#!/usr/bin/env bash
# Build the meshcom stack inside the Trixie container: qemu-system-xtensa (from source, headless),
# the MeshCom firmware images (flash.bin & co.), and the bridge. Approach A via lhpc build.
# XR_PASSWORD is @file?: (optional) — with no secret file present it builds with an empty value
# (the published firmware is OPEN-AUTH; lhpc's binary channel runs the bridge accordingly).
set -euo pipefail

ROOT=/build/root
DIST=/out
mkdir -p "$ROOT/src" "$DIST"
export LHPC_RUNTIME_ROOT="$ROOT"
source /builders/lib-build.sh
source /builders/headless-policy.sh

read -r Q_REMOTE Q_PIN Q_PATH <<<"$(read_src meshcom-qemu)"
read -r B_REMOTE B_PIN B_PATH <<<"$(read_src meshcom-bridge)"
COMMIT="${SOURCE_COMMIT:-$Q_PIN}"
echo "meshcom-qemu   : $Q_REMOTE @ $COMMIT -> $Q_PATH"
echo "meshcom-bridge : $B_REMOTE @ $B_PIN -> $B_PATH"

echo "==> Clone sources"
git clone --quiet "$Q_REMOTE" "$ROOT/$Q_PATH"
git -C "$ROOT/$Q_PATH" fetch --quiet origin "$COMMIT" 2>/dev/null || true
git -C "$ROOT/$Q_PATH" -c advice.detachedHead=false checkout --quiet "$COMMIT"
git clone --quiet "$B_REMOTE" "$ROOT/$B_PATH"
git -C "$ROOT/$B_PATH" -c advice.detachedHead=false checkout --quiet "$B_PIN"
HEAD="$(git -C "$ROOT/$Q_PATH" rev-parse HEAD)"
[ "$HEAD" = "$COMMIT" ] || { echo "HEAD $HEAD != requested $COMMIT" >&2; exit 4; }

echo "==> lhpc build meshcom (qemu-from-source + firmware + bridge — slowest)"
if ! "$LHPC" build meshcom --yes; then
  echo "=== lhpc build log (tail) ==="
  cat "$ROOT"/logs/build-meshcom*.log 2>/dev/null | tail -120 || true
  exit 5
fi

QEMU_BIN="$(ls "$ROOT"/build/tool-cache/qemu-xtensa/*/qemu/bin/qemu-system-xtensa 2>/dev/null | head -1)"
FLASH="$(ls "$ROOT/$Q_PATH"/.work/MeshCom-Firmware/.pio/build/*/flash.bin 2>/dev/null | head -1)"
BRIDGE="$ROOT/$B_PATH/build/meshcom-loraham-bridge"
[ -x "$QEMU_BIN" ] || { echo "FAIL: qemu-system-xtensa not built" >&2; exit 5; }
[ -f "$FLASH" ]    || { echo "FAIL: flash.bin not built" >&2; exit 5; }
[ -x "$BRIDGE" ]   || { echo "FAIL: bridge not built" >&2; exit 5; }
file "$QEMU_BIN"; file "$BRIDGE"; ls -la "$FLASH"
"$QEMU_BIN" --version 2>&1 | head -1 || true

run_smoke meshcom

echo "==> Runtime deps (qemu + bridge; unowned libraries are a hard failure)"
mapfile -t DEPS < <({ deps_of "$QEMU_BIN"; deps_of "$BRIDGE"; } | sort -u)
require_owned_deps meshcom "${DEPS[@]:-}"
printf 'runtime_deps: %s\n' "${DEPS[*]:-<none>}"
headless_deps_check meshcom "${DEPS[@]:-}"
ldd_closure_check meshcom "$QEMU_BIN" "$BRIDGE"

echo "==> Pack (qemu install dir + firmware *.bin + marker + bridge; runtime-root relative)"
STAGE="$(mktemp -d)"
# qemu install prefix (bin + share/pc-bios)
QEMU_DIR="$(dirname "$(dirname "$QEMU_BIN")")"          # .../qemu
QREL="${QEMU_DIR#"$ROOT"/}"
mkdir -p "$STAGE/$QREL"; cp -a "$QEMU_DIR/." "$STAGE/$QREL/"
# firmware flash images + the build marker (co-located with flash.bin); NOT the object tree
FLASH_DIR="$(dirname "$FLASH")"; FREL="${FLASH_DIR#"$ROOT"/}"
mkdir -p "$STAGE/$FREL"
cp -a "$FLASH_DIR"/*.bin "$STAGE/$FREL/"
[ -f "$FLASH_DIR/.lhpc-build-complete" ] && cp "$FLASH_DIR/.lhpc-build-complete" "$STAGE/$FREL/" || true
# bridge binary
install -D "$BRIDGE" "$STAGE/$B_PATH/build/meshcom-loraham-bridge"

COMPONENTS="{\"meshcom-qemu\": \"$COMMIT\", \"meshcom-bridge\": \"$B_PIN\"}"
pack_and_fragment meshcom "$STAGE" "$COMMIT" "$COMPONENTS" "$SMOKE_MODE" "$SMOKE_RESULT" "${DEPS[@]:-}"
write_provenance meshcom
