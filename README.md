# lhpc-binaries

Prebuilt binaries for the **long-compiling** [lhpc](https://github.com/makrohard/loraham-pi-control)
stacks, so a Raspberry Pi can install them in seconds instead of compiling for hours.

Target: **aarch64 / Debian 13 (Trixie) / glibc 2.41** (Raspberry Pi OS on Pi Zero 2 W, Pi 4, Pi 5).

## What's here
- `.github/workflows/build.yml` — one-click builder (**Actions → build-binary → Run workflow**).
- `builders/` — the per-stack build recipes (run inside a `debian:trixie` aarch64 container).
- Binaries themselves are **GitHub Release assets** (not in the git tree), described by `index.json`.

## How a binary is produced
The workflow runs on GitHub's native **arm64** runner and compiles inside a **`debian:trixie`
container** (so binaries link the exact libraries the Pi has). It installs lhpc (pure Python, no
build) and drives lhpc's own build at the chosen `source_commit`, so the artifact is byte-for-byte
the path a Pi would compile — just done once, centrally. Then it runs safety gates, derives the
runtime libraries, packs a `.tar.zst`, and publishes a **rolling** release + `index.json`.

## Trust
Binaries are served from this **public** repo over HTTPS and verified by **sha256** (recorded in
`index.json`) on the Pi — the same trust anchor lhpc already uses for source. No signing keys.

## Updating a binary
Actions → **build-binary** → Run workflow → pick the `stack` + `source_commit` → Run. That's it —
no controller change, no manifest edit, no keys.

## Scope
`meshtastic` (meshtasticd, headless), `meshcom` (qemu-system-xtensa + firmware + bridge), `daemon`.
The trivial single-file and Python stacks stay source-built.
