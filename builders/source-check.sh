#!/usr/bin/env bash
# source-check.sh: run OFFLINE (docker --network none) in the build image plus build deps. Proves the
# meshcom QEMU source companion in /out is complete: it rebuilds with downloads disabled, and the
# rebuilt emulator boots the artifact's own firmware. The binary sha comparison is informational.
set -euo pipefail
OUT=/out
srcs=("$OUT"/meshcom-qemu-source-*.tar.zst); [ ${#srcs[@]} -eq 1 ] && [ -f "${srcs[0]}" ] || { echo "FAIL: no single source companion in $OUT" >&2; exit 7; }
arts=("$OUT"/meshcom-[0-9a-f]*.tar.zst); [ ${#arts[@]} -eq 1 ] || { echo "FAIL: no single meshcom artifact" >&2; exit 7; }
W="$(mktemp -d)"
tar --zstd -xf "${srcs[0]}" -C "$W"
if curl -s --max-time 5 https://github.com >/dev/null 2>&1; then echo "FAIL: network is reachable — this check must run offline" >&2; exit 7; fi
bash "$W/meshcom-qemu-source/lhpc-build/rebuild-from-source.sh" | tee "$W/rebuild.log"
SRC="$(sed -n 's/^build_source_dir=//p' "$W/meshcom-qemu-source/SOURCE-METADATA")"
Q="$(dirname "$SRC")/build/qemu-system-xtensa"
"$Q" --version | head -1
"$Q" -machine help | grep -qE '(^| )esp32( |$)' || { echo "FAIL: rebuilt QEMU lacks the esp32 machine" >&2; exit 7; }
mkdir -p "$W/art"; tar --zstd -xf "${arts[0]}" -C "$W/art" --wildcards './src/meshcom-qemu-raspi/.work/*/flash.bin'
FLASH="$(find "$W/art" -name flash.bin | head -1)"; cp "$FLASH" "$W/flash.bin"
timeout 240 "$Q" -L "$SRC/pc-bios" -nographic -machine esp32 -m 4M -drive "file=$W/flash.bin,if=mtd,format=raw" \
  -nic user,model=open_eth -global driver=timer.esp32.timg,property=wdt_disable,value=true \
  -serial "file:$W/uart.log" -monitor none & QP=$!
ok=0; for _ in $(seq 1 110); do grep -qE 'CLIENT STARTED|Console started on port 2323' "$W/uart.log" 2>/dev/null && { ok=1; break; }; sleep 2; done
kill "$QP" 2>/dev/null || true; wait "$QP" 2>/dev/null || true
[ "$ok" = 1 ] || { tail -30 "$W/uart.log" >&2; echo "FAIL: the rebuilt QEMU did not boot the artifact's firmware" >&2; exit 7; }
echo "SOURCE-CHECK: PASS (offline rebuild from the companion; the rebuilt QEMU boots the artifact's firmware)"
grep -E '^(rebuilt|recorded):' "$W/rebuild.log" || true
