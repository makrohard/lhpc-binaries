#!/usr/bin/env bash
# THE per-stack smoke tests, callable from BOTH stages so they can never drift:
#   * build stage      — proves the freshly built artifacts run at all;
#   * runtime-test     — proves them again on a CLEAN image with ONLY runtime_deps installed.
#
#   smoke.sh <stack> <runtime-root>
#
# Exit codes: 7 = boot/exec failure, 8 = web GUI unreachable. Prints "SMOKE: PASS ..." on success.
set -euo pipefail

STACK="${1:?stack}"
ROOT="${2:?runtime root}"

smoke_daemon() {
  local bin="$ROOT/src/loraham-daemon/loraham_daemon/loraham_daemon" out
  [ -x "$bin" ] || { echo "FAIL: daemon binary not at $bin" >&2; exit 7; }
  # `--version` proves the dynamic loader resolves every .so AT RUNTIME and main() runs —
  # no radio hardware needed. The daemon is a Unix-socket service; there is no web check.
  if ! out="$("$bin" --version 2>&1)"; then
    echo "FAIL: daemon --version did not exit 0" >&2
    # Honest classification: a missing library is an environment/packaging problem, not a build bug.
    grep -iE "error while loading shared|cannot open shared object" <<<"$out" >&2 || echo "$out" >&2
    exit 7
  fi
  echo "$out"
  grep -q 'loraham_daemon' <<<"$out" || { echo "FAIL: unexpected --version output" >&2; exit 7; }
  echo "SMOKE: PASS (daemon executes on target userland)"
}

smoke_meshtastic() {
  local mtd="$ROOT/build/tools/meshtasticd/meshtasticd"
  local webroot="$ROOT/build/tools/meshtasticd/web"
  [ -x "$mtd" ] || { echo "FAIL: meshtasticd not at $mtd" >&2; exit 7; }
  "$mtd" --version 2>&1 | sed -n '1p' || true
  echo "web root: $webroot"; ls "$webroot" 2>/dev/null | head -5 || echo "(web root missing!)"
  local sm; sm="$(mktemp -d)"
  # `Lora: Module: sim` = SimRadio (no SPI/GPIO); explicit MAC because the container has no
  # usable interface (else exit 8 "Please set a MAC Address"). Self-generated SSL cert.
  cat > "$sm/config.yaml" <<YAML
---
Lora:
  Module: sim
Webserver:
  Port: 9443
  RootPath: $webroot
  SSLKey: $sm/key.pem
  SSLCert: $sm/cert.pem
Logging:
  LogLevel: info
General:
  MACAddress: "02:00:00:00:00:01"
YAML
  HOME="$sm" "$mtd" -c "$sm/config.yaml" >"$sm/meshtasticd.log" 2>&1 &
  local mpid=$! web=0 code
  echo "meshtasticd pid $mpid — polling https://127.0.0.1:9443/"
  for _ in $(seq 1 40); do
    if ! kill -0 "$mpid" 2>/dev/null; then echo "meshtasticd exited early"; break; fi
    code="$(curl -k -s -o "$sm/body.html" -w '%{http_code}' --max-time 3 https://127.0.0.1:9443/ 2>/dev/null || true)"
    if [ "$code" = "200" ] && grep -qiE 'meshtastic|<!doctype html|<html' "$sm/body.html"; then
      web=1; echo "web GUI 200 ($(wc -c <"$sm/body.html") bytes)"; break
    fi
    sleep 2
  done
  kill -TERM "$mpid" 2>/dev/null || true; sleep 1; kill -KILL "$mpid" 2>/dev/null || true
  if [ "$web" != "1" ]; then
    echo "=== meshtasticd log (tail) ==="; tail -50 "$sm/meshtasticd.log" 2>/dev/null || true
    echo "FAIL: meshtastic web GUI not reachable on :9443" >&2; exit 8
  fi
  echo "SMOKE: PASS (meshtasticd boots as sim node; web GUI reachable on :9443)"
}

smoke_meshcom() {
  # Boots the firmware under the packaged qemu via the repo's own run.sh. The meshcom-qemu
  # CLONE must exist in the root (build stage: cloned by the builder; runtime stage:
  # runtime-test.sh clones it at the recorded component commit — exactly what lhpc will do).
  local qpath
  qpath="$(ls -d "$ROOT"/src/meshcom-qemu-raspi 2>/dev/null | head -1)"
  [ -n "$qpath" ] || { echo "FAIL: meshcom-qemu clone missing under $ROOT/src" >&2; exit 7; }
  local qemu_bin
  qemu_bin="$(ls "$ROOT"/build/tool-cache/qemu-xtensa/*/qemu/bin/qemu-system-xtensa 2>/dev/null | head -1)"
  [ -x "$qemu_bin" ] || { echo "FAIL: packaged qemu-system-xtensa missing" >&2; exit 7; }
  "$qemu_bin" --version 2>&1 | head -1 || true
  (
    cd "$qpath"
    mkdir -p .run
    scripts/run.sh --qemu "$qemu_bin" --env qemu-headless-extradio-gpsd >.run/smoke-run.log 2>&1 &
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

  # --- bridge: exec + STARTUP path (the packaged bridge was only existence+ldd checked before) ---
  # `--backend fake` is the built-in bounded backend, so no real LoRaHAM daemon or MeshCom endpoint
  # is needed: the bridge must execute (dynamic link OK), then actually come up and serve TCP.
  local bridge="$ROOT/src/meshcom-loraham-bridge/build/meshcom-loraham-bridge"
  [ -x "$bridge" ] || { echo "FAIL: bridge not at $bridge" >&2; exit 7; }
  echo "bridge version: $("$bridge" --version 2>&1 | head -1)"
  "$bridge" --version >/dev/null 2>&1 || { echo "FAIL: bridge --version did not exit 0" >&2; exit 7; }
  local blog; blog="$(mktemp)"
  "$bridge" --backend fake --bind 127.0.0.1 --port 7018 >"$blog" 2>&1 &
  local bpid=$!
  sleep 2
  if ! kill -0 "$bpid" 2>/dev/null; then
    echo "=== bridge log ==="; cat "$blog" >&2
    echo "FAIL: bridge exited during startup" >&2; exit 7
  fi
  # It must be genuinely serving on its TCP port — bash /dev/tcp, no extra tooling.
  if ! (exec 3<>/dev/tcp/127.0.0.1/7018) 2>/dev/null; then
    kill -KILL "$bpid" 2>/dev/null || true
    echo "=== bridge log ==="; cat "$blog" >&2
    echo "FAIL: bridge not accepting TCP on :7018" >&2; exit 8
  fi
  kill -TERM "$bpid" 2>/dev/null; sleep 1; kill -KILL "$bpid" 2>/dev/null || true
  echo "SMOKE: PASS (bridge starts with fake backend + serves TCP on :7018)"
}

case "$STACK" in
  daemon)     smoke_daemon ;;
  meshtastic) smoke_meshtastic ;;
  meshcom)    smoke_meshcom ;;
  *) echo "unknown stack for smoke: $STACK" >&2; exit 2 ;;
esac
