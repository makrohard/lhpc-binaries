#!/usr/bin/env bash
# CLEAN-RUNTIME test stage (A6). Runs in a FRESH digest-pinned Trixie container WITHOUT any
# build toolchain. Proves the artifact runs with ONLY the declared runtime_deps installed —
# the build container is full of compilers and -dev packages and proves nothing about a Pi.
#
# Inputs (read-only): /out (tarball + fragment from the build job), /builders, /keyrings.
set -euo pipefail

STACK="${STACK:?}"
FRAG="/out/${STACK}.frag.json"
[ -f "$FRAG" ] || { echo "FAIL: fragment $FRAG missing" >&2; exit 2; }

echo "==> Harness tooling (test mechanics only — NOT the artifact's runtime deps)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates curl git zstd tar python3 file procps >/dev/null

echo "==> Raspberry Pi archive (runtime_deps may include RPi-specific packages)"
install -D -m 0644 /keyrings/raspberrypi-archive.asc /usr/share/keyrings/raspberrypi-archive.asc
echo "deb [signed-by=/usr/share/keyrings/raspberrypi-archive.asc] http://archive.raspberrypi.com/debian trixie main" \
  > /etc/apt/sources.list.d/raspi.list
apt-get update -qq

read_frag() { python3 -c "import json,sys; d=json.load(open('$FRAG')); print(d$1)"; }
FNAME="$(read_frag "['filename']")"
SHA="$(read_frag "['sha256']")"
DEPS="$(python3 -c "import json; print(' '.join(json.load(open('$FRAG'))['runtime_deps']))")"
echo "artifact: $FNAME"
echo "declared runtime_deps: ${DEPS:-<none>}"

echo "==> Install ONLY the declared runtime_deps"
[ -z "$DEPS" ] || apt-get install -y --no-install-recommends $DEPS >/dev/null

echo "==> Verify + extract into a clean runtime root"
ROOT=/rt/root
mkdir -p "$ROOT"
echo "$SHA  /out/$FNAME" | sha256sum -c - >/dev/null || { echo "FAIL: tarball sha256 mismatch" >&2; exit 3; }
if [ "$STACK" = "meshcom" ]; then
  # The meshcom artifact overlays a PINNED clone (run scripts live in the repo) — recreate
  # exactly what lhpc's binary channel will do on the Pi: clone at the recorded component
  # commit FIRST, then extract the overlay on top.
  QCOMMIT="$(read_frag "['components']['meshcom-qemu']")"
  git clone --quiet https://github.com/makrohard/meshcom-qemu-raspi.git "$ROOT/src/meshcom-qemu-raspi"
  git -C "$ROOT/src/meshcom-qemu-raspi" fetch --quiet origin "$QCOMMIT" 2>/dev/null || true
  git -C "$ROOT/src/meshcom-qemu-raspi" -c advice.detachedHead=false checkout --quiet "$QCOMMIT"
fi
tar --zstd -C "$ROOT" -xf "/out/$FNAME"

echo "==> ELF dependency closure on the CLEAN image"
source /builders/headless-policy.sh
mapfile -t ELFS < <(find "$ROOT" -type f -exec sh -c 'file -b "$1" | grep -q "^ELF" && echo "$1"' _ {} \;)
echo "ELF binaries in artifact: ${#ELFS[@]}"
[ "${#ELFS[@]}" -gt 0 ] || { echo "FAIL: no ELF binaries found in artifact" >&2; exit 3; }
ldd_closure_check "$STACK-clean" "${ELFS[@]}"

echo "==> Every resolved library must come from runtime_deps or the documented base image"
# The harness itself installs curl/git/python3/file/procps and their libraries. Without this
# check a MISSING declared dependency can be silently satisfied by a harness package, and the
# artifact then fails on a real Pi that installed only runtime_deps (audit finding).
BASE_ALLOW="libc6 libgcc-s1 libstdc++6 zlib1g libzstd1 liblzma5 libbz2-1.0 libselinux1 libpcre2-8-0"
bad=0
for elf in "${ELFS[@]}"; do
  while read -r so; do
    [ -n "$so" ] || continue
    pkg="$(dpkg -S "$(readlink -f "$so" 2>/dev/null || echo "$so")" 2>/dev/null | cut -d: -f1 | head -1)"
    [ -n "$pkg" ] || { echo "FAIL: $so is owned by no package" >&2; bad=1; continue; }
    case " $DEPS $BASE_ALLOW " in
      *" $pkg "*) ;;
      *) echo "FAIL: $elf needs $pkg (via $so), which is NOT in runtime_deps — it was only" >&2
         echo "      present because the TEST HARNESS installed it." >&2; bad=1 ;;
    esac
  done < <(ldd "$elf" 2>/dev/null | grep -oE '/[^ ]+\.so[.0-9]*' | sort -u)
done
[ "$bad" -eq 0 ] || exit 9
echo "runtime_deps closure: complete"

if [ "${SMOKE_TEST:-true}" = "false" ]; then
  echo "RUNTIME-TEST: extraction + ELF closure OK; smoke skipped (diagnostic build)"
  exit 0
fi

echo "==> The SAME smoke test, on the clean image"
bash /builders/smoke.sh "$STACK" "$ROOT"
echo "RUNTIME-TEST: PASS (clean image + declared runtime_deps only)"
