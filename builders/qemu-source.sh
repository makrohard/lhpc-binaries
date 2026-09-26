#!/usr/bin/env bash
# qemu-source.sh <meshcom-qemu-raspi checkout> <packaged qemu-system-xtensa> <dist dir>
# GPLv2 §3 source companion for a MeshCom artifact that ships QEMU: the COMPLETE patched source tree
# (Espressif tag + patches/qemu/*.patch applied + the meson subprojects the build compiles in), the
# scripts that control the build, and SOURCE-METADATA. Written deterministically to
# <dist>/meshcom-qemu-source-<sha256>.tar.zst; prints that file name on stdout.
set -euo pipefail
QR="$1"; QBIN="$2"; DIST="$3"
BQ="$QR/scripts/build-qemu.sh"
val() { sed -n "s/^$1=\"\\([^\"]*\\)\".*/\\1/p" "$BQ" | head -1; }   # value up to the closing quote
Q_REMOTE="$(val QEMU_REMOTE)"; Q_TAG="$(val QEMU_TAG)"; Q_COMMIT="$(val QEMU_COMMIT | cut -c1-40)"
[ -n "$Q_REMOTE" ] && [ -n "$Q_TAG" ] && [ ${#Q_COMMIT} -eq 40 ] || { echo "FAIL: cannot read the QEMU pin from $BQ" >&2; exit 6; }

W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
S="$W/meshcom-qemu-source"; mkdir -p "$S/lhpc-build/patches"
echo "==> QEMU source companion: $Q_REMOTE $Q_TAG ($Q_COMMIT)" >&2
git clone --quiet --depth 1 --branch "$Q_TAG" "$Q_REMOTE" "$S/qemu"
[ "$(git -C "$S/qemu" rev-parse HEAD)" = "$Q_COMMIT" ] || { echo "FAIL: QEMU tag does not resolve to the pinned commit" >&2; exit 6; }
for p in "$QR"/patches/qemu/*.patch; do
  [ -f "$p" ] || continue
  git -C "$S/qemu" apply "$p"; cp "$p" "$S/lhpc-build/patches/"
done
# The subprojects QEMU's configure downloads and compiles into this binary (the others are in-tree).
( cd "$S/qemu" && meson subprojects download berkeley-softfloat-3 berkeley-testfloat-3 dtc keycodemapdb >&2 )
rm -rf "$S/qemu/.git" "$S"/qemu/subprojects/*/.git

cp "$BQ" "$QR/scripts/lib-publish.sh" "$S/lhpc-build/"
GATE="$("$PY" -c 'import lhpc, os; print(os.path.join(os.path.dirname(lhpc.__file__), "data/scripts/meshtastic-link-gate.sh"))')"
cp "$GATE" "$S/lhpc-build/"
# The binary embeds its build paths (prefix, and the build's work tree through __FILE__); a
# byte-identical rebuild has to use the same ones.
PREFIX="$(dirname "$(dirname "$QBIN")")"
SRCDIR="$(strings "$QBIN" | grep -oE '/[^ ]*/\.qemu-work\.[A-Za-z0-9_.-]+' | sort -u | head -1)/qemu"
# The exact configure arguments: evaluate build-qemu.sh's own array definition (comments dropped).
ARGS="$(TARGET_LIST="$(val TARGET_LIST)"; eval "$(sed -n '/^CONFIGURE_FEATURE_ARGS=(/,/^)/p' "$BQ" | grep -vE '^[[:space:]]*#')"; printf '%s ' "${CONFIGURE_FEATURE_ARGS[@]}")"
[ -n "$ARGS" ] || { echo "FAIL: cannot read the configure arguments from $BQ" >&2; exit 6; }
{
  echo "qemu-system-xtensa in this MeshCom artifact is GPL-2.0 (COPYING in qemu/)."
  echo "upstream_remote=$Q_REMOTE"
  echo "upstream_tag=$Q_TAG"
  echo "upstream_commit=$Q_COMMIT"
  for p in "$S"/lhpc-build/patches/*.patch; do [ -f "$p" ] && echo "patch=$(basename "$p") sha256=$(sha256sum "$p" | cut -d' ' -f1)"; done
  echo "meshcom_qemu_raspi_commit=$(git -C "$QR" rev-parse HEAD)"
  echo "lhpc_commit=${LHPC_COMMIT:-unknown}"
  echo "container=${CONTAINER_DIGEST:-unknown}"
  echo "configure_args=$ARGS"
  echo "build_prefix=$PREFIX"
  echo "build_source_dir=$SRCDIR"
  echo "binary_sha256=$(sha256sum "$QBIN" | cut -d' ' -f1)"
  echo "rebuild: lhpc-build/rebuild-from-source.sh (offline, same container and paths; the result is not guaranteed to be bit-identical)"
} > "$S/SOURCE-METADATA"
cat > "$S/lhpc-build/rebuild-from-source.sh" <<'RB'
#!/usr/bin/env bash
# Rebuild qemu-system-xtensa from this archive, offline, at the paths recorded in SOURCE-METADATA.
# Run as root in the recorded container (debian trixie + the build-qemu.sh build deps).
set -euo pipefail
H="$(cd "$(dirname "$0")/.." && pwd)"; m() { sed -n "s/^$1=//p" "$H/SOURCE-METADATA"; }
SRC="$(m build_source_dir)"; PREFIX="$(m build_prefix)"
mkdir -p "$(dirname "$SRC")"; rm -rf "$SRC"; cp -a "$H/qemu" "$SRC"
B="$(dirname "$SRC")/build"; rm -rf "$B"; mkdir -p "$B"
if ! command -v libgcrypt-config >/dev/null 2>&1; then          # the same shim build-qemu.sh uses
  mkdir -p "$B/shim"; printf '#!/bin/sh\ncase "$1" in --version) pkg-config --modversion libgcrypt;; --cflags) pkg-config --cflags libgcrypt;; --libs) pkg-config --libs libgcrypt;; --prefix) pkg-config --variable=prefix libgcrypt;; --exec-prefix) pkg-config --variable=exec_prefix libgcrypt;; *) echo "";; esac\n' > "$B/shim/libgcrypt-config"
  chmod +x "$B/shim/libgcrypt-config"; export PATH="$B/shim:$PATH"; fi
# shellcheck disable=SC2046
( cd "$B" && "$SRC/configure" --prefix="$PREFIX" --disable-download $(m configure_args) )
( cd "$B" && ninja qemu-system-xtensa )
cp "$B/qemu-system-xtensa" "$B/qemu-system-xtensa.stripped"; strip "$B/qemu-system-xtensa.stripped"
echo "rebuilt:  $(sha256sum "$B/qemu-system-xtensa.stripped" | cut -d' ' -f1)"
echo "recorded: $(m binary_sha256)"
RB
chmod +x "$S/lhpc-build/rebuild-from-source.sh"
cp "$S/qemu/COPYING" "$S/COPYING"

T="$W/src.tar.zst"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --zstd -C "$W" -cf "$T" meshcom-qemu-source
SHA="$(sha256sum "$T" | cut -d' ' -f1)"
NAME="meshcom-qemu-source-$SHA.tar.zst"
mv "$T" "$DIST/$NAME"
echo "$NAME"
