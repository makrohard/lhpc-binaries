#!/usr/bin/env bash
# The meshtastic-web lab smoke, the web entry's mandatory gate. Runs in the runtime-test job's
# FRESH Trixie container (called by runtime-test.sh). The daemon is the meshtasticd LHPC itself
# publishes: the newest index-v3 meshtastic entry built from the commit the fragment names (the
# lhpc_ref manifest pin; the publisher binds that pin), run on SimRadio with RootPath = the client
# under test. Headless Chromium must then show the node's long name over the HTTP API
# (smoke_web.py), and the same client with its main bundle truncated must FAIL the same check.
#
# Inputs (read-only): /out (tarball + fragment), /builders; env INDEX_V3_URL.
set -euo pipefail

FRAG=/out/meshtastic-web.frag.json
INDEX_V3_URL="${INDEX_V3_URL:?}"
fv() { python3 -c "import json,sys; d=json.load(open('$FRAG')); print(d$1)"; }

echo "==> Browser tooling (test mechanics only; the client has no runtime_deps)"
apt-get install -y --no-install-recommends chromium python3-venv >/dev/null
python3 -m venv /opt/pw
/opt/pw/bin/pip install --quiet --require-hashes --only-binary=:all: -r /builders/smoke-web-requirements.txt
echo "chromium: $(chromium --version 2>/dev/null || echo '?')"

echo "==> The client under test"
FNAME="$(fv "['filename']")"
echo "$(fv "['sha256']")  /out/$FNAME" | sha256sum -c - >/dev/null || { echo "FAIL: client tarball sha256 mismatch" >&2; exit 3; }
mkdir -p /rt/web && tar --zstd -C /rt/web -xf "/out/$FNAME"
SERVED=/rt/web/build/tools/meshtastic-web/web
[ -f "$SERVED/index.html" ] || { echo "FAIL: no index.html in the served tree" >&2; exit 3; }

echo "==> The meshtasticd it is paired with (index-v3, the fragment's meshtastic commit)"
PIN="$(fv "['components']['meshtastic']")"
curl -fsSL --retry 3 -o /rt/index-v3.json "$INDEX_V3_URL"
read -r MURL MSHA MDEPS < <(python3 - "$PIN" <<'PY'
import json, sys
sys.path.insert(0, "/builders")
from lib_index import load_index_v3
from lib_web import pick_daemon_entry
idx, err = load_index_v3(json.load(open("/rt/index-v3.json")))
if err:
    sys.exit("FAIL: index-v3: " + err)
e = pick_daemon_entry(idx, sys.argv[1])
if e is None:
    sys.exit(f"FAIL: index-v3 has no meshtastic entry for {sys.argv[1]} (publish the daemon first)")
print(e["url"], e["sha256"], ",".join(e["runtime_deps"]) or "-")
PY
)
echo "meshtasticd $PIN: $MURL"
curl -fsSL --retry 3 -o /rt/mtd.tar.zst "$MURL"
echo "$MSHA  /rt/mtd.tar.zst" | sha256sum -c - >/dev/null || { echo "FAIL: meshtasticd tarball sha256 mismatch" >&2; exit 3; }
if [ "$MDEPS" != "-" ]; then
  read -ra PKGS <<<"${MDEPS//,/ }"          # names the validator allowed: [A-Za-z0-9.+-]
  apt-get install -y --no-install-recommends "${PKGS[@]}" >/dev/null
fi
mkdir -p /rt/root && tar --zstd -C /rt/root -xf /rt/mtd.tar.zst
MTD=/rt/root/build/tools/meshtasticd/meshtasticd
[ -x "$MTD" ] || { echo "FAIL: meshtasticd not in its artifact" >&2; exit 3; }
"$MTD" --version 2>&1 | sed -n '1p' || true

# One fresh SimRadio node per run (own HOME = own state), serving `$1`; the same config smoke.sh uses.
DPID=""
start_daemon() {
  local root="$1" home; home="$(mktemp -d)"
  cat > "$home/config.yaml" <<YAML
---
Lora:
  Module: sim
Webserver:
  Port: 9443
  RootPath: $root
  SSLKey: $home/key.pem
  SSLCert: $home/cert.pem
Logging:
  LogLevel: info
General:
  MACAddress: "02:00:00:00:00:01"
YAML
  DLOG="$home/meshtasticd.log"
  HOME="$home" "$MTD" -c "$home/config.yaml" >"$DLOG" 2>&1 &
  DPID=$!
  for _ in $(seq 1 40); do
    curl -ks -o /dev/null --max-time 3 https://127.0.0.1:9443/ && return 0
    kill -0 "$DPID" 2>/dev/null || break
    sleep 1
  done
  tail -30 "$DLOG" >&2; echo "FAIL: meshtasticd did not serve on :9443" >&2; exit 8
}
stop_daemon() { kill -TERM "$DPID" 2>/dev/null || true; sleep 1; kill -KILL "$DPID" 2>/dev/null || true; wait "$DPID" 2>/dev/null || true; }
smoke() { SMOKE_BROWSER=/usr/bin/chromium /opt/pw/bin/python /builders/smoke_web.py https://127.0.0.1:9443/ 127.0.0.1:9443 "$DLOG" 120; }

echo "==> Smoke: the client must show the node's long name over the HTTP API"
start_daemon "$SERVED"
if ! smoke; then stop_daemon; tail -30 "$DLOG" >&2; exit 10; fi
stop_daemon

echo "==> Control: the same client with its main bundle truncated must FAIL the same check"
cp -a "$SERVED" /rt/broken
BUNDLE="$(python3 -c 'import sys; sys.path.insert(0, "/builders")
from lib_web import main_bundle
print(main_bundle(open(sys.argv[1]).read()) or "")' /rt/broken/index.html)"
[ -n "$BUNDLE" ] || { echo "FAIL: no main bundle to truncate" >&2; exit 3; }
truncate -s "$(( $(stat -c %s "/rt/broken/$BUNDLE") / 2 ))" "/rt/broken/$BUNDLE"
start_daemon /rt/broken
rc=0; smoke || rc=$?
stop_daemon
[ "$rc" -eq 10 ] || { echo "FAIL: the broken-client control did not fail the smoke (rc $rc)" >&2; exit 11; }
echo "control: the truncated client failed as it must"
echo "WEB-SMOKE: PASS ($(fv "['channel']") $(fv "['web_version']") with meshtasticd $PIN)"
