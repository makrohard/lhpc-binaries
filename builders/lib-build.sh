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

# pack_and_fragment <stack> <stage_dir> <principal_commit> <smoke_mode> <smoke_result> <deps...>
# Packs the stage dir into a CONTENT-ADDRESSED tar.zst in $DIST and writes the v2 fragment.
pack_and_fragment() {
  local stack="$1" stage="$2" commit="$3" smoke_mode="$4" smoke_result="$5"
  shift 5
  local tmp="$DIST/.${stack}.tar.zst.tmp"
  tar --zstd -C "$stage" -cf "$tmp" .
  local sha size fname
  sha="$(sha256sum "$tmp" | cut -d' ' -f1)"
  size="$(stat -c %s "$tmp")"
  fname="${stack}-${sha}.tar.zst"
  mv "$tmp" "$DIST/$fname"
  # `components` = the EXACT manifest composition (ALL source components), derived from the ONE
  # provenance helper the trusted publisher also uses — so its independent re-derivation matches.
  local components
  components="$(BUILDERS_DIR="${BUILDERS_DIR:-/builders}" "$PY" - "$stack" "${SOURCE_COMMIT:-}" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ.get("BUILDERS_DIR", "/builders"))
from lib_provenance import derive
_, comps = derive(sys.argv[1], sys.argv[2])
print(json.dumps(comps, sort_keys=True, separators=(",", ":")))
PY
)"
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

# Run `lhpc build <stack>`, and on failure leave BOUNDED evidence for the release bot.
#
# An automated release freezes an upstream pin on this answer, so the question "was that failure
# this stack's own?" is asked of the CONTROLLER at the exact commit we were told to build — the
# same rule the release-verification lane applies to the same typed output. A second copy of it
# in shell would drift, and the direction it drifts in is freezing a pin over a broken package
# index. Everything unrecognised stays unclassified: no file is written, and the bot reports an
# ordinary failure.
#
# The evidence BINDS itself to this execution. A marker alone says "some meshtastic build broke";
# the bot must be able to tell that it was THIS dispatch, of THIS candidate.
#
# The filename is visible on purpose: `upload-artifact` drops dotfiles by default, so a
# `.regression` member would have been written, uploaded into nothing, and silently never read.
build_stack() {
  # `$OUT_DIR` is the artifact hand-off directory; the container mounts it at /out. Named rather
  # than hard-coded so this can be driven outside the container by its own test.
  local stack="$1" cap rc out_dir="${OUT_DIR:-/out}"
  cap="$(mktemp)"
  set +e
  "$LHPC" build "$stack" --yes 2>&1 | tee "$cap"
  rc="${PIPESTATUS[0]}"
  set -e
  [ "$rc" = 0 ] && { rm -f "$cap"; return 0; }

  echo "=== lhpc build log (tail) ==="
  cat "$ROOT"/logs/build-"$stack"*.log 2>/dev/null | tail -100 || true

  local marker
  marker="$("$PY" "${LHPC_SRC_DIR:-/opt/lhpc-src}/tools/build_regression.py" "$stack" "$cap" || true)"
  if [ -n "$marker" ]; then
    {
      printf '%s\n' "$marker"
      printf 'builder-run: %s\n' "${GITHUB_RUN_ID:-unknown}"
      printf 'builder-attempt: %s\n' "${GITHUB_RUN_ATTEMPT:-unknown}"
      printf 'lhpc-commit: %s\n' "${LHPC_COMMIT:-unknown}"
    } > "${out_dir}/${stack}.regression"
    echo "==> wrote ${out_dir}/${stack}.regression — this stack's OWN build step failed"
  else
    echo "==> no regression evidence: nothing here says the recipe broke"
  fi
  rm -f "$cap"
  return "$rc"
}
