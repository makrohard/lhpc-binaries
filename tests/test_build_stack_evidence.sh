#!/usr/bin/env bash
# `build_stack` must leave evidence a release can freeze a pin on — and ONLY then.
#
# The PRODUCER half of the attribution contract, driven the way the container drives it: a real
# `lhpc build` failure shape on stdout, the real controller tool deciding, and the real file
# written into the hand-off directory. The bot's consumer is tested against these exact bytes.
set -euo pipefail
BINARIES="$(cd "$(dirname "$0")/.." && pwd)"
LHPC_SRC="${LHPC_SRC:-$HOME/claude/lhpc-auto-release-docs}"
if [ ! -f "$LHPC_SRC/tools/build_regression.py" ]; then
  # A guard that exits 0 when its subject is missing is not a guard. In CI this is fatal: the
  # checkout is part of the contract, and this test once "passed" in CI having run nothing,
  # because the controller checkout predated the release that added the tool.
  if [ -n "${CI:-}" ]; then
    echo "FAIL: no controller tool at $LHPC_SRC/tools/build_regression.py — CI must check out a" >&2
    echo "      controller that has it; this contract may not pass by skipping." >&2
    exit 1
  fi
  echo "SKIP: no controller checkout at $LHPC_SRC (set LHPC_SRC)"; exit 0
fi

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

# ---- a failure that a TRANSFER ended is repeated, at most twice; nothing else is ----
export BUILD_TRANSFER_WAITS="0 0"
STEPLOG="$TMP/root/logs/build-meshcore-node-1.log"
repeat_case() {  # why, failures before a pass, step-log text, expected calls, expected rc (0|red), extra check
  # "red" means lhpc's OWN exit status (7 here) comes back: a red reached any other way — the
  # repeat loop failing on its own, say — is not the build's failure and does not pass.
  local why="$1" fails="$2" text="$3" calls="$4" want="$5" grepfor="${6:-}" got rc log="$TMP/run.log"
  rm -f "$TMP/out/meshcore.regression" "$TMP/calls"
  printf '%s' "$text" > "$STEPLOG"
  cat > "$LHPC" <<SH
#!/usr/bin/env bash
echo x >> "$TMP/calls"
[ "\$(wc -l < "$TMP/calls")" -gt $fails ] && exit 0
echo "  [failed] build meshcore-node (rc 7, log $STEPLOG)"
exit 7
SH
  chmod +x "$LHPC"
  set +e; ( source "$LIB"; build_stack meshcore ) > "$log" 2>&1; rc=$?; set -e
  got="$(wc -l < "$TMP/calls")"
  [ "$got" = "$calls" ] || { echo "FAIL: $why — $got calls, want $calls"; cat "$log"; exit 1; }
  if [ "$want" = 0 ]; then [ "$rc" = 0 ] || { echo "FAIL: $why — rc $rc"; exit 1; }
  else [ "$rc" = 7 ] || { echo "FAIL: $why — rc $rc, want lhpc's own 7"; cat "$log"; exit 1; }; fi
  [ -z "$grepfor" ] || grep -qF -- "$grepfor" "$log" || { echo "FAIL: $why — no '$grepfor'"; cat "$log"; exit 1; }
  echo "  ok: $why"
}
PIO="PackageException: Got the unrecognized status code '504' when downloaded https://github.com/arduino-libraries/NTPClient/archive/refs/tags/3.2.1.zip"
repeat_case "a PlatformIO 504 that ended the build is repeated once and passes" 1 "$PIO"$'\n' 2 0 \
  "==> transfer failure ended lhpc build, repeating in 0 s (1/2): $PIO"
repeat_case "a meson 503 under its two wrapper lines is repeated" 1 \
  $'fatal: unable to access \'https://gitlab.com/q/k.git/\': The requested URL returned error: 503\n../qemu/ui/meson.build:175:22: ERROR: Git command failed: [fetch]\nERROR: meson setup failed\n' 2 0
repeat_case "a recovered transfer followed by a compile error is not repeated" 1 \
  $'curl: (28) Operation timed out\nsrc/main.cpp:12:5: error: \'radio\' was not declared\n' 1 red
repeat_case "a 404 is not repeated" 1 \
  $'fatal: unable to access \'https://github.com/x/y.git/\': The requested URL returned error: 404\n' 1 red
repeat_case "a transfer every time: two repeats, then red as before" 9 "$PIO"$'\n' 3 red \
  "==> no regression evidence"
BUILD_TRANSFER_WAITS="0" repeat_case "one wait listed: one repeat" 9 "$PIO"$'\n' 2 red "(1/2)"
BUILD_TRANSFER_WAITS="0 0 0" repeat_case "three waits listed: still two repeats at most" 9 \
  "$PIO"$'\n' 3 red "(2/2)"
BUILD_T0=$(( $(date +%s) - 6000 )) repeat_case "no time left: not repeated" 1 "$PIO"$'\n' 1 red \
  "==> no time left for a repeat"

# An attributed own-step failure writes evidence and is never repeated, transfer line or not.
rm -f "$TMP/out/meshcore.regression" "$TMP/calls"
printf '%s\n' "$PIO" > "$TMP/root/logs/$(basename "$OWNED")"
printf '#!/usr/bin/env bash\necho x >> %q\necho %q\nexit 1\n' "$TMP/calls" \
  "  [failed] build x (rc 1, log $TMP/root/logs/$(basename "$OWNED"))" > "$LHPC"; chmod +x "$LHPC"
( source "$LIB"; build_stack meshcore ) >/dev/null 2>&1 || true
[ -f "$TMP/out/meshcore.regression" ] || { echo "FAIL: own-step failure — no evidence"; exit 1; }
[ "$(wc -l < "$TMP/calls")" = 1 ] || { echo "FAIL: own-step failure was repeated"; exit 1; }
echo "  ok: an attributed own-step failure writes evidence and is not repeated"
unset BUILD_TRANSFER_WAITS
echo "build_stack evidence: all cases pass"
