# Plan — lhpc-binaries builder

## Goal
Let a Raspberry Pi install the heavy stacks as verified downloads instead of compiling them,
and let the maintainer publish new binaries **without any change to the lhpc controller repo**.

## Model (simple + safe)
- **Public repo**, binaries as **GitHub Release assets** (never in the git tree → no clone bloat).
- **Rolling release**: one release whose per-stack assets are overwritten each build; `index.json`
  (a stable asset) always points at the current ones.
- **Trust = GitHub-over-HTTPS + sha256** in `index.json` (same anchor lhpc uses for source). No keys.
- **Manual `workflow_dispatch`** trigger → building only when the maintainer clicks = the rate limit.

## Build (approach A — zero drift)
Run on `ubuntu-24.04-arm`; compile inside a `debian:trixie` container (exact target libs).
Install lhpc (Python, no build) and drive **`lhpc build <stack>`** at the requested `source_commit`,
so the CI artifact is the same path a Pi compiles. Collect → gate → derive deps → pack → publish.

Key unknown to validate on the daemon first: how to make `lhpc build` target an arbitrary
`source_commit` (via lhpc's dev/remote/known-working override). Fallback: replicate the stack's own
build scripts.

## Per stack (verified artifact map)
| stack | ships | extract-to |
|---|---|---|
| daemon | `loraham_daemon` (RadioLib linked in) | `src/loraham-daemon/loraham_daemon/` |
| meshtastic | `meshtasticd` + `web/` | `build/tools/meshtasticd/` |
| meshcom | `qemu-system-xtensa` + `flash.bin` + `bridge` | tool-cache + `.work/…` + bridge build |

## Gates (all blocking)
link-gate (no X11/SDL) · graphics denylist on derived deps · `--version` smoke · `HEAD==source_commit`.

## Runtime deps
`ldd` each shipped binary → `dpkg -S` → runtime `.so` packages only (no `-dev`, no toolchain).

## index.json (the contract lhpc consumes)
```json
{ "<stack>": { "url": "...", "sha256": "...", "built_from": "<commit>",
               "runtime_deps": ["..."], "target": "aarch64-trixie" } }
```

## Milestones
1. plumbing-check green (arm64 + trixie + lhpc install).
2. daemon end-to-end (validates approach A) → real tarball + index.json.
3. meshtastic (headless), meshcom (needs an `xr_pw` secret).

## Known follow-ups
Pin GitHub Actions to commit SHAs; retention/rollback policy (rolling = latest only); the meshcom
build secret.
