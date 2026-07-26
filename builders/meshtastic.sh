#!/usr/bin/env bash
# Build headless meshtasticd (PlatformIO env `native`) + web assets inside the Trixie container.
# Approach A: lhpc build runs the real recipe, INCLUDING the link-gate build step that proves no
# X11/SDL linkage. We add a graphics-package denylist on the derived runtime deps as a second gate.
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

echo "==> Runtime deps"
echo "--- raw ldd ---"; ldd "$MTD" 2>&1 | sed -n '1,40p' || true
mapfile -t DEPS < <(
  ldd "$MTD" 2>/dev/null | grep -oE '/[^ ]+\.so[.0-9]*' | sort -u \
  | while read -r so; do
      dpkg -S "$(readlink -f "$so" 2>/dev/null || echo "$so")" 2>/dev/null | cut -d: -f1
    done | sort -u
)
printf 'runtime_deps: %s\n' "${DEPS[*]:-<none>}"
# Graphics denylist — the headless server build must never pull these.
if printf '%s\n' "${DEPS[@]:-}" | grep -qiE 'libx11|libsdl|libgtk|mesa|libgl1|wayland|pulse|libxcb'; then
  echo "FAIL: graphics runtime dependency detected — refusing" >&2; exit 6
fi

if [ "${SMOKE_TEST:-true}" != "false" ]; then
  echo "==> Smoke gate: meshtasticd boots as a sim node and its web GUI is reachable"
  # No radio in CI. `Lora: Module: sim` selects meshtastic's SimRadio (use_simradio), which skips all
  # SPI/GPIO init, so the node boots fully headless and the web server comes up. (`-s` would force sim
  # but short-circuits `-c`, so we drive sim through the config instead.) SSL cert is self-generated.
  "$MTD" --version 2>&1 | sed -n '1p' || true
  WEBROOT="$ROOT/build/tools/meshtasticd/web"
  echo "web root: $WEBROOT"; ls "$WEBROOT" 2>/dev/null | head -8 || echo "(web root missing!)"
  SM="$(mktemp -d)"
  cat > "$SM/config.yaml" <<YAML
---
Lora:
  Module: sim
Webserver:
  Port: 9443
  RootPath: $WEBROOT
  SSLKey: $SM/key.pem
  SSLCert: $SM/cert.pem
Logging:
  LogLevel: info
General:
  # On a Pi meshtasticd derives the node MAC from eth0; the CI container has no usable
  # interface, so it exits 8 ("Please set a MAC Address") without an explicit one.
  MACAddress: "02:00:00:00:00:01"
YAML
  HOME="$SM" "$MTD" -c "$SM/config.yaml" >"$SM/meshtasticd.log" 2>&1 &
  MPID=$!
  echo "meshtasticd pid $MPID — polling https://127.0.0.1:9443/ for the web GUI"
  web=0
  for _ in $(seq 1 40); do
    if ! kill -0 "$MPID" 2>/dev/null; then echo "meshtasticd exited early"; break; fi
    code="$(curl -k -s -o "$SM/body.html" -w '%{http_code}' --max-time 3 https://127.0.0.1:9443/ 2>/dev/null || true)"
    if [ "$code" = "200" ] && grep -qiE 'meshtastic|<!doctype html|<html' "$SM/body.html"; then
      web=1; echo "web GUI 200 ($(wc -c <"$SM/body.html") bytes)"; break
    fi
    sleep 2
  done
  kill -TERM "$MPID" 2>/dev/null || true; sleep 1; kill -KILL "$MPID" 2>/dev/null || true
  if [ "$web" != "1" ]; then
    echo "=== meshtasticd log (tail) ==="; tail -50 "$SM/meshtasticd.log" 2>/dev/null || true
    echo "FAIL: meshtastic web GUI not reachable on :9443" >&2; exit 8
  fi
  echo "SMOKE: PASS (meshtasticd boots as sim node; web GUI reachable on :9443)"
fi

echo "==> Pack (meshtasticd + web assets + build marker; runtime-root relative)"
STAGE="$(mktemp -d)"
mkdir -p "$STAGE/build/tools/meshtasticd"
cp -a "$ROOT/build/tools/meshtasticd/." "$STAGE/build/tools/meshtasticd/"
MK="$ROOT/$M_PATH/.lhpc-build-complete"
[ -f "$MK" ] && install -D "$MK" "$STAGE/$M_PATH/.lhpc-build-complete" || true
tar --zstd -C "$STAGE" -cf "$DIST/meshtastic.tar.zst" .
SHA="$(sha256sum "$DIST/meshtastic.tar.zst" | cut -d' ' -f1)"
DEPS_JSON="$(printf '%s\n' "${DEPS[@]:-}" | sed '/^$/d' | sed 's/.*/"&"/' | paste -sd, -)"
cat > "$DIST/meshtastic.frag.json" <<EOF
{"stack":"meshtastic","sha256":"$SHA","built_from":"$COMMIT","runtime_deps":[${DEPS_JSON}],"target":"aarch64-trixie","extract_to":"runtime-root"}
EOF
echo "PACKED meshtastic.tar.zst  sha256=$SHA"; cat "$DIST/meshtastic.frag.json"; echo
