#!/usr/bin/env bash
# Shared build-stage helpers, sourced by the per-stack builders (inside the container).
# Requires: $PY, $LHPC, $LHPC_COMMIT, $BUILDER_COMMIT, $CONTAINER_DIGEST, $ROOT, $DIST.

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

# Derive runtime packages for one ELF: ldd -> dpkg -S. A library that cannot be mapped to a
# package is a HARD failure (the audit found silent drops could ship an artifact whose declared
# runtime_deps are insufficient on a clean Pi).
deps_of() {
  local elf="$1" so pkg
  ldd "$elf" 2>/dev/null | grep -oE '/[^ ]+\.so[.0-9]*' | sort -u \
  | while read -r so; do
      pkg="$(dpkg -S "$(readlink -f "$so" 2>/dev/null || echo "$so")" 2>/dev/null | cut -d: -f1)"
      if [ -z "$pkg" ]; then
        echo "UNOWNED:$so"
      else
        echo "$pkg"
      fi
    done | sort -u
}

# Fail if any dependency line is an UNOWNED marker.
require_owned_deps() {
  local name="$1"; shift
  if printf '%s\n' "$@" | grep -q '^UNOWNED:'; then
    echo "FAIL(${name}): shared libraries not owned by any package — runtime_deps would lie:" >&2
    printf '%s\n' "$@" | grep '^UNOWNED:' >&2
    exit 6
  fi
}

# pack_and_fragment <stack> <stage_dir> <principal_commit> <components_json> <smoke_mode> <smoke_result> <deps...>
# Packs the stage dir into a CONTENT-ADDRESSED tar.zst in $DIST and writes the v2 fragment.
pack_and_fragment() {
  local stack="$1" stage="$2" commit="$3" components="$4" smoke_mode="$5" smoke_result="$6"
  shift 6
  local tmp="$DIST/.${stack}.tar.zst.tmp"
  tar --zstd -C "$stage" -cf "$tmp" .
  local sha size fname
  sha="$(sha256sum "$tmp" | cut -d' ' -f1)"
  size="$(stat -c %s "$tmp")"
  fname="${stack}-${sha}.tar.zst"
  mv "$tmp" "$DIST/$fname"
  local deps_json
  deps_json="$(printf '%s\n' "$@" | sed '/^$/d' | sed 's/.*/"&"/' | paste -sd, -)"
  cat > "$DIST/${stack}.frag.json" <<EOF
{
  "stack": "${stack}",
  "filename": "${fname}",
  "sha256": "${sha}",
  "size": ${size},
  "built_from": "${commit}",
  "components": ${components},
  "lhpc_commit": "${LHPC_COMMIT}",
  "builder_commit": "${BUILDER_COMMIT:-unknown}",
  "target": "aarch64-trixie",
  "os": "trixie",
  "container_digest": "${CONTAINER_DIGEST:-unknown}",
  "runtime_deps": [${deps_json}],
  "smoke": {"mode": "${smoke_mode}", "result": "${smoke_result}"},
  "extract_to": "runtime-root"
}
EOF
  echo "== fragment =="; cat "$DIST/${stack}.frag.json"
  echo "PACKED ${fname}  sha256=${sha}  size=${size}"
}

# Plain-text provenance beside the artifact (deliberately NOT a subsystem: two package lists).
write_provenance() {
  local stack="$1"
  {
    echo "# provenance for ${stack} — builder ${BUILDER_COMMIT:-unknown}, lhpc ${LHPC_COMMIT}"
    echo "# container ${CONTAINER_DIGEST:-unknown}"
    echo "## dpkg -l"
    dpkg -l 2>/dev/null
    echo "## pip freeze (lhpc venv)"
    /opt/lhpcvenv/bin/pip freeze 2>/dev/null
  } > "$DIST/${stack}.provenance.txt"
}

# Smoke wrapper honoring the diagnostic switch; records mode/result for the fragment.
# Usage: run_smoke <stack>  — sets SMOKE_MODE / SMOKE_RESULT.
run_smoke() {
  local stack="$1"
  if [ "${SMOKE_TEST:-true}" = "false" ]; then
    SMOKE_MODE="skipped"; SMOKE_RESULT="skipped"
    echo "==> Smoke SKIPPED (diagnostic build — publish will refuse this artifact)"
    return 0
  fi
  SMOKE_MODE="mandatory"
  bash /builders/smoke.sh "$stack" "$ROOT"
  SMOKE_RESULT="passed"
}
