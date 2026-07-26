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
"$MTD" --version >/dev/null 2>&1 || "$MTD" --help >/dev/null 2>&1 \
  || echo "(no --version/--help; skipping smoke — non-fatal)"

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
