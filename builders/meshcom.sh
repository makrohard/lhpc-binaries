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
build_stack meshcom || exit 5

# The artifact is LABELLED with the manifest's meshcom-firmware pin (lib_provenance derives the
# components map from the manifest). Until lhpc 0.9.2 the QEMU build fetched a hardcoded commit,
# so the label and the bytes disagreed for every artifact since 0.2.10 (finding R8). Refuse to
# pack anything whose firmware checkout is not the pin: the label must be the truth, not a copy
# of the manifest.
read -r _FW_REMOTE FW_PIN _FW_PATH <<<"$(read_src meshcom-firmware)"
FW_WORK="$ROOT/$Q_PATH/.work/MeshCom-Firmware"
FW_HEAD="$(git -C "$FW_WORK" rev-parse HEAD 2>/dev/null || echo missing)"
if [ "$FW_HEAD" != "$FW_PIN" ]; then
  echo "FAIL: firmware checkout in $FW_WORK is $FW_HEAD but the manifest pins meshcom-firmware at $FW_PIN — the artifact would be labelled with a commit it does not contain" >&2
  exit 5
fi
echo "==> firmware checkout $FW_HEAD == manifest pin (label is the truth)"

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
# QEMU is GPL-2.0: the artifact must carry its licence and source note (installed by build-qemu.sh)
for f in COPYING SOURCE; do
  [ -f "$QEMU_DIR/share/doc/qemu/$f" ] || { echo "FAIL: $QEMU_DIR/share/doc/qemu/$f missing — the artifact would ship QEMU without its GPL text/source note" >&2; exit 5; }
done
QREL="${QEMU_DIR#"$ROOT"/}"
mkdir -p "$STAGE/$QREL"; cp -a "$QEMU_DIR/." "$STAGE/$QREL/"
# firmware flash images + the build marker (co-located with flash.bin); NOT the object tree
FLASH_DIR="$(dirname "$FLASH")"; FREL="${FLASH_DIR#"$ROOT"/}"
mkdir -p "$STAGE/$FREL"
cp -a "$FLASH_DIR"/*.bin "$STAGE/$FREL/"
# The completion marker is what the controller reads to decide the stack counts as built, and it
# now also carries the build inputs the controller compares against. An artifact shipped without
# it leaves every box reading "not built" with no recovery but a republish — so its absence is a
# build failure here, never a silent omission.
MK="$FLASH_DIR/.lhpc-build-complete"
[ -f "$MK" ] || { echo "FAIL: build marker not at $MK — publishing without it would make every box read meshcom as not built" >&2; exit 5; }
cp "$MK" "$STAGE/$FREL/"
# The build-input SIDECAR lives beside the artifact (same directory as flash.bin): since lhpc 0.7.0
# it records the packaged assets the build consumed, and `is_built` compares it byte for byte. Ask
# the CONTROLLER whether it records anything for this stack; if it does and no sidecar reached the
# stage, that is a build failure — an artifact without it reads NOT built on every 0.7.0 box with
# no recovery but a republish (exactly what the cfec05e proof build did).
SIDE="$FLASH_DIR/.lhpc-build-inputs"
RECORDS="$("$PY" -c "
from lhpc.core.manifest import load_manifest
print(any(c.build_inputs or getattr(c, 'asset_inputs', ()) for st in load_manifest()
          if st.id == 'meshcom' for c in st.components))")"
if [ "$RECORDS" = "True" ]; then
  [ -f "$SIDE" ] || { echo "FAIL: this controller records build inputs for meshcom but $SIDE is missing" >&2; exit 5; }
  cp "$SIDE" "$STAGE/$FREL/"
fi
# bridge binary
install -D "$BRIDGE" "$STAGE/$B_PATH/build/meshcom-loraham-bridge"

pack_and_fragment meshcom "$STAGE" "$COMMIT" "$SMOKE_MODE" "$SMOKE_RESULT" "${DEPS[@]:-}"
write_provenance meshcom
