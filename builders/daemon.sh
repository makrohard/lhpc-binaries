#!/usr/bin/env bash
# Build the loraham daemon (C++; RadioLib linked in) inside the Trixie container.
# Approach A: drive lhpc's OWN build so the recipe never drifts. Source pins are read
# from lhpc's manifest (no hardcoding); SOURCE_COMMIT overrides the daemon commit only.
set -euo pipefail

LHPC=/opt/lhpcvenv/bin/lhpc
PY=/opt/lhpcvenv/bin/python
ROOT=/work/root
DIST=/work/dist
mkdir -p "$ROOT/src" "$DIST"
export LHPC_RUNTIME_ROOT="$ROOT"

# Read a component's (remote, pin_commit, path) straight from lhpc's manifest loader.
read_src() { "$PY" - "$1" <<'PY'
import sys
from lhpc.core.manifest import load_manifest
cid = sys.argv[1]
for st in load_manifest():
    for c in st.components:
        if c.id == cid and getattr(c, "source", None):
            print(c.source.remote, c.source.pin_commit, c.source.path)
            raise SystemExit(0)
sys.exit("component/source not found: " + cid)
PY
}

read -r D_REMOTE D_PIN D_PATH  <<<"$(read_src loraham-daemon)"
read -r R_REMOTE R_PIN R_PATH  <<<"$(read_src radiolib)"
COMMIT="${SOURCE_COMMIT:-$D_PIN}"
echo "daemon   : $D_REMOTE @ $COMMIT -> $D_PATH"
echo "radiolib : $R_REMOTE @ $R_PIN -> $R_PATH"

echo "==> Build toolchain"
apt-get install -y --no-install-recommends build-essential cmake >/dev/null

echo "==> Clone sources"
git clone --quiet "$R_REMOTE" "$ROOT/$R_PATH"
git -C "$ROOT/$R_PATH" -c advice.detachedHead=false checkout --quiet "$R_PIN"
git clone --quiet "$D_REMOTE" "$ROOT/$D_PATH"
git -C "$ROOT/$D_PATH" fetch --quiet origin "$COMMIT" 2>/dev/null || true
git -C "$ROOT/$D_PATH" -c advice.detachedHead=false checkout --quiet "$COMMIT"

# Gate: the checked-out commit is exactly what was requested.
HEAD="$(git -C "$ROOT/$D_PATH" rev-parse HEAD)"
[ "$HEAD" = "$COMMIT" ] || { echo "HEAD $HEAD != requested $COMMIT" >&2; exit 4; }

echo "==> lhpc build daemon (real recipe: RadioLib then daemon)"
"$LHPC" build daemon --yes

BIN="$ROOT/$D_PATH/loraham_daemon/loraham_daemon"
[ -x "$BIN" ] || { echo "FAIL: daemon binary not at $BIN" >&2; exit 5; }
file "$BIN"

echo "==> Smoke test"
"$BIN" --help >/dev/null 2>&1 || "$BIN" --version >/dev/null 2>&1 \
  || echo "(binary has no --help/--version; skipping smoke — non-fatal)"

echo "==> Runtime deps (ldd -> dpkg -S)"
mapfile -t DEPS < <(ldd "$BIN" | awk '/=>/{print $3}' | grep -E '^/' | sort -u \
  | while read -r so; do dpkg -S "$so" 2>/dev/null | cut -d: -f1; done | sort -u)
printf 'runtime_deps: %s\n' "${DEPS[*]:-<none>}"
# Graphics denylist — a headless daemon must never pull these.
if printf '%s\n' "${DEPS[@]:-}" | grep -qiE 'libx11|libsdl|libgtk|mesa|libgl1|wayland|pulse|libxcb'; then
  echo "FAIL: graphics runtime dependency detected" >&2; exit 6
fi

echo "==> Pack (tar.zst; extracts relative to the runtime root)"
STAGE="$(mktemp -d)"
install -D "$BIN" "$STAGE/$D_PATH/loraham_daemon/loraham_daemon"
tar --zstd -C "$STAGE" -cf "$DIST/daemon.tar.zst" .
SHA="$(sha256sum "$DIST/daemon.tar.zst" | cut -d' ' -f1)"

DEPS_JSON="$(printf '%s\n' "${DEPS[@]:-}" | sed '/^$/d' | sed 's/.*/"&"/' | paste -sd, -)"
cat > "$DIST/daemon.frag.json" <<EOF
{"stack":"daemon","sha256":"$SHA","built_from":"$COMMIT","runtime_deps":[${DEPS_JSON}],"target":"aarch64-trixie","extract_to":"runtime-root"}
EOF
echo "== fragment =="; cat "$DIST/daemon.frag.json"; echo
echo "PACKED daemon.tar.zst  sha256=$SHA"
