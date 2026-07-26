#!/usr/bin/env bash
# Runs INSIDE the digest-pinned debian:trixie aarch64 container. Sets up the build environment,
# installs lhpc (pure Python — no compile), then dispatches to the per-stack builder.
#
# SECURITY: this stage executes UNTRUSTED upstream build systems. The container therefore sees
# only read-only /builders + /keyrings and a writable /out — no git checkout of this repo, no
# credentials of any kind (the workflow's build job runs with contents: read and a
# persist-credentials:false checkout on the host side).
set -euo pipefail

echo "==> Environment"
uname -m
sed -n 's/^PRETTY_NAME=//p' /etc/os-release
# `| head` under `set -o pipefail` is SIGPIPE-racy (exit 141); sed reads the whole stream.
ldd --version 2>&1 | sed -n '1p'
echo "builder_commit: ${BUILDER_COMMIT:-<unset>}  container: ${CONTAINER_DIGEST:-<unset>}"

echo "==> Base tooling"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  git ca-certificates curl python3 python3-venv python3-pip xz-utils zstd file \
  build-essential cmake >/dev/null
# build-essential (gcc/g++/make) + cmake are UNIVERSAL compile prerequisites. lhpc's per-stack
# manifest deps assume a shared system where build-essential is already present (declared by the
# daemon stack; installed system-wide by bootstrap-deps' merged transaction) — meshtastic's own
# `require` list does NOT include gcc, so a per-stack build must provide the base toolchain here.

echo "==> Raspberry Pi archive (match the Pi's userland: Debian Trixie + archive.raspberrypi.com)"
# The target is Raspberry Pi OS = Debian + the RPi archive, which ships RPi-specific packages
# (e.g. liblgpio-dev, used by RadioLib's Pi HAL) that vanilla debian:trixie lacks. The signing key
# is vendored in this repo (the authentic 'Raspberry Pi Archive Signing Key', fingerprint
# CF8A1AF5...7FA3303E) so we add no trust and fetch no keys at build time.
install -D -m 0644 /keyrings/raspberrypi-archive.asc /usr/share/keyrings/raspberrypi-archive.asc
echo "deb [signed-by=/usr/share/keyrings/raspberrypi-archive.asc] http://archive.raspberrypi.com/debian trixie main" \
  > /etc/apt/sources.list.d/raspi.list
apt-get update -qq

echo "==> Install lhpc — EXACT ref resolution, no fallback (${LHPC_REF:-main})"
# PUBLISHING builds (smoke gate armed) require an IMMUTABLE recipe: a full 40-hex
# loraham-pi-control commit SHA. Branch/tag refs are allowed only for smoke_test=false
# diagnostic builds, which can never enter the release.
if [ "${STACK:-}" != "plumbing-check" ] && [ "${SMOKE_TEST:-true}" != "false" ] \
   && ! [[ "${LHPC_REF:-main}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "FAIL: publishing builds require an exact loraham-pi-control commit SHA as lhpc_ref" >&2
  echo "      (got '${LHPC_REF:-main}'). Use smoke_test=false for diagnostic branch builds." >&2
  exit 4
fi
# Any branch, tag or full SHA resolves through one honest path: fetch the requested ref, detach
# at the RESOLVED commit, assert, print, and RECORD it in the fragment. A typo or unknown ref
# FAILS the build — it can never silently become default main.
git clone --quiet --no-checkout https://github.com/makrohard/loraham-pi-control.git /opt/lhpc-src
git -C /opt/lhpc-src fetch --quiet origin "${LHPC_REF:-main}"
LHPC_COMMIT="$(git -C /opt/lhpc-src rev-parse --verify FETCH_HEAD^{commit})"
git -C /opt/lhpc-src -c advice.detachedHead=false checkout --quiet "$LHPC_COMMIT"
if [[ "${LHPC_REF:-main}" =~ ^[0-9a-f]{40}$ ]] && [ "$LHPC_COMMIT" != "${LHPC_REF}" ]; then
  echo "FAIL: resolved lhpc commit $LHPC_COMMIT != requested ${LHPC_REF}" >&2; exit 4
fi
export LHPC_COMMIT
echo "lhpc recipe commit: $LHPC_COMMIT"
python3 -m venv /opt/lhpcvenv
/opt/lhpcvenv/bin/pip install --quiet --upgrade pip
/opt/lhpcvenv/bin/pip install --quiet /opt/lhpc-src
export LHPC="/opt/lhpcvenv/bin/lhpc"
export PY="/opt/lhpcvenv/bin/python"
echo "lhpc: $($LHPC --version)"

# Install exactly the apt packages lhpc DECLARES for this stack (build + runtime -dev libs),
# extracted from the manifest so the builder never drifts from lhpc's own dependency list.
install_declared_deps() {
  local stack="$1" pkgs
  pkgs="$("$PY" - "$stack" <<'PY'
import sys
from lhpc.core.manifest import load_manifest
stack = sys.argv[1]; out = []
for st in load_manifest():
    if st.id != stack:
        continue
    for c in st.components:
        for r in getattr(c, "requires", ()):
            ins = (r.install or "")
            if "apt" in ins and "install" in ins:
                toks = ins.split()
                for t in toks[toks.index("install") + 1:]:
                    if not t.startswith("-"):
                        out.append(t)
print(" ".join(dict.fromkeys(out)))
PY
)"
  echo "declared deps for '${stack}': ${pkgs:-<none>}"
  [ -z "$pkgs" ] || apt-get install -y --no-install-recommends $pkgs >/dev/null
}

echo "==> Dispatch: stack=${STACK} source_commit=${SOURCE_COMMIT:-<manifest pin>}"
case "${STACK}" in
  plumbing-check)
    echo "PLUMBING OK — arm64 + Trixie + lhpc installed. No stack built."
    ;;
  daemon|meshtastic|meshcom)
    install_declared_deps "${STACK}"
    if [ -f "/builders/${STACK}.sh" ]; then
      exec bash "/builders/${STACK}.sh"
    fi
    echo "builder for '${STACK}' not implemented yet" >&2
    exit 3
    ;;
  *)
    echo "unknown stack: ${STACK}" >&2
    exit 2
    ;;
esac
