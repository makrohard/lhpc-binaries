#!/usr/bin/env bash
# Runs INSIDE a debian:trixie aarch64 container. Sets up the build environment,
# installs lhpc (pure Python — no compile), then dispatches to the per-stack
# builder. Kept deliberately small; each stack's specifics live in builders/<stack>.sh.
set -euo pipefail

echo "==> Environment"
uname -m
grep -E '^PRETTY_NAME' /etc/os-release
ldd --version | head -1

echo "==> Base tooling"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  git ca-certificates curl python3 python3-venv python3-pip xz-utils zstd file >/dev/null

echo "==> Install lhpc (${LHPC_REF:-main}) — Python, no build"
git clone --quiet --depth 1 --branch "${LHPC_REF:-main}" \
  https://github.com/makrohard/loraham-pi-control.git /opt/lhpc-src \
  || git clone --quiet https://github.com/makrohard/loraham-pi-control.git /opt/lhpc-src
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
    if [ -x "builders/${STACK}.sh" ]; then
      exec bash "builders/${STACK}.sh"
    fi
    echo "builder for '${STACK}' not implemented yet" >&2
    exit 3
    ;;
  *)
    echo "unknown stack: ${STACK}" >&2
    exit 2
    ;;
esac
