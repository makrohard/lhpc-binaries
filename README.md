# lhpc-binaries

Prebuilt binaries for the **long-compiling** [lhpc](https://github.com/makrohard/loraham-pi-control)
stacks, so a Raspberry Pi can install them in seconds instead of compiling for hours.

Target: **aarch64 / Debian 13 (Trixie) / glibc 2.41** (Raspberry Pi OS on Pi Zero 2 W, Pi 4, Pi 5).

## What's here
- `.github/workflows/build.yml` — one-click builder (**Actions → build-binary → Run workflow**).
- `builders/` — the per-stack build recipes (run inside a `debian:trixie` aarch64 container).
- Binaries themselves are **GitHub Release assets** (not in the git tree), described by `index.json`.

## How a binary is produced (hardened three-job pipeline)
1. **build** — GitHub's native **arm64** runner, compiling inside a digest-pinned
   **`debian:trixie`** container. The container executes UNTRUSTED upstream build systems, so it
   sees only read-only builder scripts and a separate output directory — **no git checkout, no
   credentials** (the job runs `contents: read` with a credential-free checkout). It installs
   lhpc (pure Python) at an EXACTLY resolved ref (no fallback — a typo fails the build) and
   drives lhpc's own build recipe at the chosen `source_commit`. A mandatory smoke test gates
   the artifact (`smoke_test=false` produces a build-only diagnostic that can never be
   published). Result leaves as a workflow artifact.
2. **runtime-test** — a FRESH Trixie container withOUT any build toolchain installs ONLY the
   declared `runtime_deps`, extracts the tarball into a clean root, checks the full ELF
   dependency closure, and runs the SAME smoke test again — proving the artifact runs on a
   clean Pi, not just in the build environment.
3. **publish** — the only job with `contents: write`, serialized across ALL stacks by a global
   concurrency group. It re-validates and RE-HASHES everything (never executing anything from
   the artifact), uploads the **content-addressed** asset `<stack>-<sha256>.tar.zst` (immutable,
   old assets retained) plus a plain-text provenance file, saves the index it is about to
   replace as `index.prev.json`, and uploads `index.json` LAST as the pointer switch. A
   transient index-read failure aborts the publish — it can never wipe the other stacks'
   entries.

## index.json (schema 2)
`{schema: 2, stacks: {<stack>: {filename, url, sha256, size, built_from,
components: {<lhpc component id>: <commit>}, lhpc_commit, builder_commit, target, os,
container_digest, runtime_deps, smoke: {mode, result}, extract_to}}}` — the `components` map is
the authoritative pin comparison for lhpc's binary channel; `built_from` is display-only.

## Trust
Binaries are served from this **public** repo over HTTPS and verified by **sha256 + size**
(recorded in `index.json`) on the Pi — the same trust anchor lhpc already uses for source. No
signing keys. Every artifact records the exact source commits, lhpc recipe commit, builder
commit and container digest it was produced from.

## Updating a binary
Actions → **build-binary** → Run workflow → pick the `stack`, paste an **exact 40-hex
loraham-pi-control commit SHA** as `lhpc_ref` (the immutable recipe a publishing build is built
from), optionally set `source_commit`, and Run. That's it — no controller change, no manifest edit,
no keys.

`lhpc_ref` is **required** and, for a build that publishes, must be a full commit SHA — the recipe
is pinned into the artifact's provenance. A branch/tag is accepted **only** with `smoke_test=false`,
which is a build-only diagnostic run that never reaches the release.

## Rolling back

Every artifact ever published is still there (content-addressed, never clobbered) and the index
each publish replaced is kept as `index.prev.json`. So pointing a stack back at an earlier
build is a POINTER move, not a rebuild: Actions → **rollback-binary** → `stacks`
(comma-separated), optionally `snapshot_json` (the index to restore from — blank means "undo
the last publish") and `expect_json` (`{"<stack>": <the entry you published>}`).

It runs in the publisher's own concurrency group and refuses rather than guesses:

- the live entry must be the one the caller says it published — otherwise someone published in
  between, and that is reported as a **conflict**, never overwritten;
- the entry it restores must pass the same validator a publish passes, its artifact must still
  exist, and its bytes must hash to the recorded sha256;
- the index is read back after the switch and compared, so an upload that did not take effect
  is never reported as a successful rollback.

Nothing from an artifact is executed: names, bytes and hashes only.

## Scope
`meshtastic` (meshtasticd, headless), `meshcom` (qemu-system-xtensa + firmware + bridge), `daemon`.
The trivial single-file and Python stacks stay source-built.

## Licenses & attribution

The build tooling in this repo is MIT ([LICENSE](LICENSE)). The **release assets are builds of
other projects**, each under its own license; the exact source commit of every component is
recorded per artifact in `index.json` (`components` map) and in the `.provenance.txt` beside it.

- **`daemon`** — the [LoRaHAM daemon](https://github.com/makrohard/LoRaHAM_Daemon) · GPL-3.0 ·
  original author **Alexander Walter** ([LoRaHAM project](https://github.com/LoRaHAM)); includes
  [RadioLib](https://github.com/jgromes/RadioLib) (© Jan Gromeš, MIT) compiled in.
- **`meshtastic`** — `meshtasticd` built from
  [meshtastic/firmware](https://github.com/meshtastic/firmware) · © Meshtastic contributors ·
  GPL-3.0.
- **`meshcom`** — `qemu-system-xtensa` built from
  [Espressif's QEMU fork](https://github.com/espressif/qemu)
  (tag `esp-develop-9.0.0-20240606`) · GPL-2.0; the
  [MeshCom firmware](https://github.com/icssw-org/MeshCom-Firmware) (© ICSSW, MIT); and the
  [MeshCom bridge](https://github.com/makrohard/meshcom-loraham-bridge) (MIT).
