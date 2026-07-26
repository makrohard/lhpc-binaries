#!/usr/bin/env bash
# Build the meshcom stack inside the Trixie container: qemu-system-xtensa (from source, headless),
# the MeshCom firmware images (flash.bin & co.), and the bridge. Approach A via lhpc build.
# XR_PASSWORD is @file?: (optional) — with no secret file present it builds with an empty value.
set -euo pipefail

LHPC=/opt/lhpcvenv/bin/lhpc
PY=/opt/lhpcvenv/bin/python
ROOT=/work/root
DIST=/work/dist
mkdir -p "$ROOT/src" "$DIST"
export LHPC_RUNTIME_ROOT="$ROOT"

read_src() { "$PY" - "$1" <<'PY'
import sys
from lhpc.core.manifest import load_manifest
cid = sys.argv[1]
for st in load_manifest():
    for c in st.components:
        if c.id == cid and getattr(c, "source", None):
            print(c.source.remote, c.source.pin_commit, c.source.path); raise SystemExit(0)
sys.exit("no source: " + cid)
PY
}

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

if [ "${SMOKE_TEST:-true}" != "false" ]; then
  echo "==> Smoke gate: boot the firmware under the built qemu; require boot marker AND web GUI (:18083)"
  # run.sh boots the guest (backgrounds qemu, writes .run/qemu.pid + uart-latest.log, forwards the guest
  # web UI to host 127.0.0.1:18083). We keep the guest up ourselves so the web GUI can be gated (the
  # maintainer's test.sh stops the guest it boots and treats web as diagnostic-only). Entirely in-container.
  (
    cd "$ROOT/$Q_PATH"
    mkdir -p .run
    scripts/run.sh --qemu "$QEMU_BIN" --env qemu-headless-extradio-gpsd >.run/smoke-run.log 2>&1 &
    RUNPID=$!
    MARK='CLIENT STARTED|Console started on port 2323'
    booted=0
    for _ in $(seq 1 150); do
      UART="$(readlink -f .run/uart-latest.log 2>/dev/null || true)"
      if [ -n "$UART" ] && [ -f "$UART" ] && grep -qE "$MARK" "$UART"; then booted=1; break; fi
      if ! kill -0 "$RUNPID" 2>/dev/null; then echo "run.sh exited early"; break; fi
      sleep 4
    done
    if [ "$booted" != "1" ]; then
      echo "=== UART tail ==="; tail -60 "${UART:-/dev/null}" 2>/dev/null || true
      echo "=== run log ==="; tail -20 .run/smoke-run.log 2>/dev/null || true
      scripts/stop.sh >/dev/null 2>&1 || true
      echo "FAIL: firmware did not reach the boot marker" >&2; exit 7
    fi
    echo "SMOKE: boot marker seen"
    web=0
    for _ in $(seq 1 30); do
      code="$(curl -s -o .run/web.html -w '%{http_code}' --max-time 3 http://127.0.0.1:18083/ 2>/dev/null || true)"
      if [ "$code" = "200" ] && grep -qiE 'meshcom|<html|<!doctype' .run/web.html; then
        web=1; echo "web GUI 200 ($(wc -c <.run/web.html) bytes)"; break
      fi
      sleep 2
    done
    scripts/stop.sh >/dev/null 2>&1 || true
    if [ "$web" != "1" ]; then echo "FAIL: meshcom web GUI not reachable on :18083" >&2; exit 8; fi
    echo "SMOKE: PASS (meshcom boots + web GUI reachable on :18083)"
  )
fi

echo "==> Runtime deps (qemu + bridge)"
deps_of() {
  ldd "$1" 2>/dev/null | grep -oE '/[^ ]+\.so[.0-9]*' | sort -u \
    | while read -r so; do dpkg -S "$(readlink -f "$so" 2>/dev/null || echo "$so")" 2>/dev/null | cut -d: -f1; done
}
mapfile -t DEPS < <({ deps_of "$QEMU_BIN"; deps_of "$BRIDGE"; } | sort -u)
printf 'runtime_deps: %s\n' "${DEPS[*]:-<none>}"
if printf '%s\n' "${DEPS[@]:-}" | grep -qiE 'libx11|libsdl|libgtk|mesa|libgl1|wayland|pulse|libxcb'; then
  echo "FAIL: graphics runtime dependency detected — refusing" >&2; exit 6
fi

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

tar --zstd -C "$STAGE" -cf "$DIST/meshcom.tar.zst" .
SHA="$(sha256sum "$DIST/meshcom.tar.zst" | cut -d' ' -f1)"
DEPS_JSON="$(printf '%s\n' "${DEPS[@]:-}" | sed '/^$/d' | sed 's/.*/"&"/' | paste -sd, -)"
cat > "$DIST/meshcom.frag.json" <<EOF
{"stack":"meshcom","sha256":"$SHA","built_from":"$COMMIT","runtime_deps":[${DEPS_JSON}],"target":"aarch64-trixie","extract_to":"runtime-root"}
EOF
echo "PACKED meshcom.tar.zst  sha256=$SHA"; cat "$DIST/meshcom.frag.json"; echo
