#!/usr/bin/env python3
"""Point one or more stacks back at a previously published artifact — no rebuild.

The publisher keeps every artifact it ever published (content-addressed, never clobbered) and,
since the previous-index change, the index it replaced. Rolling back is therefore a POINTER
move: re-file an entry that was already built, smoke-tested, runtime-tested and validated.

What this refuses to do, and why:

  * it never rolls back a stack whose LIVE entry is not the one the caller says it published.
    An automated release records the entry its own build produced; if the live entry is
    something else, someone published in between and this would silently delete their work.
    That is a reported CONFLICT, not a rollback.
  * it never restores an entry it cannot re-validate with the SAME rules a publish passes
    (`lib_index.validate_entry`), and never one whose artifact is missing or whose bytes do
    not hash to the recorded sha256.
  * it never executes anything from an artifact. It reads names, bytes and hashes.

It runs under the publisher's own `publish` concurrency group, so it cannot interleave with a
publish of the same repository.

Env:
  STACKS         comma-separated stack ids to restore (required)
  SNAPSHOT_JSON  the index to restore FROM (optional; default: the published index.prev.json)
  EXPECT_JSON    {stack: entry} the caller published and expects to find live (optional but
                 strongly advised: without it a foreign entry cannot be told from your own)
  GITHUB_REPOSITORY, RELEASE_TAG (default "binaries")
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.environ.get("BUILDERS_DIR", "builders"))
from lib_index import SCHEMA, load_index, validate_entry  # noqa: E402

INDEX = "index.json"
PREV = "index.prev.json"
SUMS = "SHA256SUMS"


def canon(entry: dict) -> str:
    """One stable rendering of a whole entry — the unit of comparison. Comparing a handful of
    fields would let an entry with the same digest but different provenance pass as 'ours'."""
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


class Release:
    """The rolling release, through `gh`. The one seam the tests replace."""

    def __init__(self, repo: str, tag: str):
        self.repo, self.tag = repo, tag

    def _gh(self, *args: str, binary: bool = False):
        r = subprocess.run(["gh", *args], capture_output=True, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args)} failed: {r.stderr.decode()[:400]}")
        return r.stdout if binary else r.stdout.decode()

    def asset_names(self) -> list:
        out = self._gh("release", "view", self.tag, "--json", "assets", "--jq",
                       ".assets[].name")
        return out.split()

    def read_asset(self, name: str) -> bytes:
        meta = json.loads(self._gh("api", f"repos/{self.repo}/releases/tags/{self.tag}"))
        try:
            asset_id = next(a["id"] for a in meta["assets"] if a["name"] == name)
        except StopIteration:
            raise FileNotFoundError(name) from None
        return self._gh("api", "-H", "Accept: application/octet-stream",
                        f"repos/{self.repo}/releases/assets/{asset_id}", binary=True)

    def upload(self, path: Path) -> None:
        self._gh("release", "upload", self.tag, str(path), "--clobber")


def _validated(raw: bytes, what: str) -> dict:
    try:
        idx, err = load_index(json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise SystemExit(f"FAIL: {what} is not a readable index ({exc})") from None
    if err:
        raise SystemExit(f"FAIL: {what}: {err}")
    for sid, entry in sorted(idx["stacks"].items()):
        bad = validate_entry(sid, entry)
        if bad:
            raise SystemExit(f"FAIL: {what}: {bad}")
    return idx


def rollback(stacks: list, snapshot: dict, expect: dict, release: Release) -> list:
    """Restore `stacks` from `snapshot`. Returns one report line per stack. Raises SystemExit
    on any refusal — nothing is uploaded unless every named stack can be restored."""
    live = _validated(release.read_asset(INDEX), "the live index")
    names = set(release.asset_names())
    report, changes = [], {}

    for sid in stacks:
        snap_entry = snapshot["stacks"].get(sid)
        if snap_entry is None:
            raise SystemExit(f"FAIL: {sid}: the snapshot has no entry to restore")
        live_entry = live["stacks"].get(sid)

        if live_entry is not None and canon(live_entry) == canon(snap_entry):
            report.append(f"{sid}: already the snapshot entry — verified no-op")
            continue

        want = expect.get(sid)
        if want is not None and (live_entry is None or canon(live_entry) != canon(want)):
            raise SystemExit(
                f"FAIL: {sid}: the live entry is not the one this attempt published — "
                f"someone else published in between. Refusing to replace it. "
                f"live={canon(live_entry)[:160] if live_entry else 'absent'}")

        fname = snap_entry["filename"]
        if fname not in names:
            raise SystemExit(f"FAIL: {sid}: the artifact {fname} is no longer published — "
                             "it cannot be restored")
        blob = release.read_asset(fname)
        got = hashlib.sha256(blob).hexdigest()
        if got != snap_entry["sha256"] or len(blob) != snap_entry["size"]:
            raise SystemExit(f"FAIL: {sid}: {fname} does not match the snapshot "
                             f"(sha256 {got[:9]} / {len(blob)} bytes)")
        changes[sid] = snap_entry
        report.append(f"{sid}: restore {fname[:24]}… ({snap_entry['sha256'][:9]})")

    if not changes:
        return report

    merged = {"schema": SCHEMA, "stacks": dict(live["stacks"])}
    merged["stacks"].update(changes)
    for sid, entry in sorted(merged["stacks"].items()):
        bad = validate_entry(sid, entry)
        if bad:
            raise SystemExit(f"FAIL: the merged index is not publishable: {bad}")

    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        (out / INDEX).write_text(json.dumps(merged, indent=2, sort_keys=True))
        (out / SUMS).write_text("".join(
            f"{merged['stacks'][s]['sha256']}  {merged['stacks'][s]['filename']}\n"
            for s in sorted(merged["stacks"])))
        release.upload(out / SUMS)
        release.upload(out / INDEX)          # the pointer switch — LAST, as in publish
        back = _validated(release.read_asset(INDEX), "the index read back after the rollback")
        for sid, entry in changes.items():
            if canon(back["stacks"].get(sid, {})) != canon(entry):
                raise SystemExit(f"FAIL: {sid}: the index read back is not what was written — "
                                 "the rollback is NOT confirmed")
    report.append(f"index read back and confirmed for: {', '.join(sorted(changes))}")
    return report


def main() -> int:
    stacks = [s.strip() for s in os.environ.get("STACKS", "").split(",") if s.strip()]
    if not stacks:
        print("FAIL: STACKS is empty — nothing to roll back", file=sys.stderr)
        return 2
    repo = os.environ["GITHUB_REPOSITORY"]
    release = Release(repo, os.environ.get("RELEASE_TAG", "binaries"))

    raw = os.environ.get("SNAPSHOT_JSON", "").strip()
    if raw:
        snapshot = _validated(raw.encode(), "the supplied snapshot")
    else:
        try:
            snapshot = _validated(release.read_asset(PREV), f"the published {PREV}")
        except FileNotFoundError:
            print(f"FAIL: no {PREV} is published and no SNAPSHOT_JSON was given",
                  file=sys.stderr)
            return 2

    expect_raw = os.environ.get("EXPECT_JSON", "").strip()
    expect = json.loads(expect_raw) if expect_raw else {}
    if not isinstance(expect, dict):
        print("FAIL: EXPECT_JSON must be an object {stack: entry}", file=sys.stderr)
        return 2
    if not expect:
        print("WARNING: no EXPECT_JSON — a foreign publish cannot be distinguished from the "
              "entry this attempt produced. Restoring anyway, as asked.")

    for line in rollback(stacks, snapshot, expect, release):
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
