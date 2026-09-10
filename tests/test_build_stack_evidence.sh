#!/usr/bin/env bash
# `build_stack` must leave evidence a release can freeze a pin on — and ONLY then.
#
# The PRODUCER half of the attribution contract, driven the way the container drives it: a real
# `lhpc build` failure shape on stdout, the real controller tool deciding, and the real file
# written into the hand-off directory. The bot's consumer is tested against these exact bytes.
set -euo pipefail
BINARIES="$(cd "$(dirname "$0")/.." && pwd)"
LHPC_SRC="${LHPC_SRC:-$HOME/claude/lhpc-auto-release-docs}"
[ -f "$LHPC_SRC/tools/build_regression.py" ] || { echo "SKIP: no controller checkout at $LHPC_SRC"; exit 0; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/out" "$TMP/root/logs" "$TMP/bin" "$TMP/opt/lhpc-src"
cp -r "$LHPC_SRC/tools" "$LHPC_SRC/lhpc" "$TMP/opt/lhpc-src/"

export PY=python3 ROOT="$TMP/root" OUT_DIR="$TMP/out"
export LHPC_COMMIT="ac5497c69bba346109ce2319b27caf28b8f709e9"
export GITHUB_RUN_ID=4242 GITHUB_RUN_ATTEMPT=2
export LHPC="$TMP/bin/lhpc"

# The wrapper asks the controller at /opt/lhpc-src, which is where the container puts it.
export LHPC_SRC_DIR="$TMP/opt/lhpc-src"
LIB="$BINARIES/builders/lib-build.sh"

OWNED="$(PYTHONPATH="$LHPC_SRC" python3 - <<PY
import pathlib, tomllib
from lhpc.core.build_regression import own_step_logs
from lhpc.core.manifest import parse_manifest
doc = tomllib.loads(pathlib.Path("$LHPC_SRC/lhpc/data/manifest.example.toml").read_text())
st = next(s for s in parse_manifest(doc) if s.id == "meshcore")
print(sorted(own_step_logs(st))[0])
PY
)"

check() {  # name, printed line, yes|no
  local why="$1" line="$2" expect="$3" f="$TMP/out/meshcore.regression"
  rm -f "$f"
  printf '#!/usr/bin/env bash\necho %q\nexit 1\n' "$line" > "$LHPC"; chmod +x "$LHPC"
  ( source "$LIB"; build_stack meshcore ) >/dev/null 2>&1 || true
  if [ "$expect" = yes ]; then
    [ -f "$f" ] || { echo "FAIL: $why — no evidence written"; exit 1; }
    grep -qx "STACK-REGRESSION stack=meshcore phase=build" "$f" || { echo "FAIL: $why — marker"; exit 1; }
    grep -qx "builder-run: 4242" "$f"        || { echo "FAIL: $why — run id not bound"; exit 1; }
    grep -qx "builder-attempt: 2" "$f"       || { echo "FAIL: $why — attempt not bound"; exit 1; }
    grep -qx "lhpc-commit: $LHPC_COMMIT" "$f" || { echo "FAIL: $why — candidate not bound"; exit 1; }
  else
    [ ! -f "$f" ] || { echo "FAIL: $why — evidence written when it must not be"; exit 1; }
  fi
  echo "  ok: $why"
}

check "a step the recipe declares its own"    "  [failed] build x (rc 1, log /l/$OWNED)"                          yes
check "a networked pip step"                  "  [failed] build meshcore-node (rc 1, log /l/build-meshcore-node-1.log)" no
check "a build that ran out of time"          "  [timeout] build meshcore-node (rc 124, log /l/$OWNED)"           no
check "a completion-marker write failure"     "  [failed] build meshcore-node (rc 1, log )"                       no
check "a refusal before any step ran"         "ERR   Refusing to build: not installed."                           no
check "no output at all"                      ""                                                                  no

# A SUCCESSFUL build writes nothing and returns 0.
printf '#!/usr/bin/env bash\nexit 0\n' > "$LHPC"; chmod +x "$LHPC"
( source "$LIB"; build_stack meshcore ) >/dev/null 2>&1
[ ! -f "$TMP/out/meshcore.regression" ] || { echo "FAIL: a green build wrote evidence"; exit 1; }
echo "  ok: a successful build writes nothing"
echo "build_stack evidence: all cases pass"
