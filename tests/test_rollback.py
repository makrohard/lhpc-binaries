"""Rolling a stack back to an artifact that is already published.

Every case here is a failure path the automated pin release depends on: it publishes candidate
binaries BEFORE its proof runs, so when the proof fails the index must go back to exactly what
it was — and must refuse when that is not provably safe.

Run with: python3 -m pytest tests/
"""

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "builders"))

from rollback import canon, rollback  # noqa: E402

SHA_A = hashlib.sha256(b"artifact-A").hexdigest()
SHA_B = hashlib.sha256(b"artifact-B").hexdigest()
SHA_C = hashlib.sha256(b"artifact-C").hexdigest()


def _entry(sid, sha, blob, lhpc="c" * 40):
    return {"filename": f"{sid}-{sha}.tar.zst",
            "url": f"https://github.com/o/r/releases/download/binaries/{sid}-{sha}.tar.zst",
            "sha256": sha, "size": len(blob), "built_from": "x",
            "components": {sid: "b" * 40},
            "lhpc_commit": lhpc, "builder_commit": "d" * 40, "target": "aarch64-trixie",
            "os": "trixie", "container_digest": "debian@sha256:" + "e" * 64,
            "runtime_deps": ["libgpiod2"], "smoke": {"mode": "mandatory", "result": "passed"},
            "extract_to": "runtime-root"}


class FakeRelease:
    """The rolling release as a name -> bytes store. `frozen` models an upload that silently
    does not take effect, which the read-back must catch."""

    def __init__(self, assets: dict, frozen: bool = False):
        self.assets = dict(assets)
        self.frozen = frozen
        self.uploaded = []

    def asset_names(self):
        return sorted(self.assets)

    def read_asset(self, name):
        if name not in self.assets:
            raise FileNotFoundError(name)
        return self.assets[name]

    def upload(self, path):
        self.uploaded.append(path.name)
        if not self.frozen:
            self.assets[path.name] = path.read_bytes()


def _index(entries):
    return {"schema": 2, "stacks": entries}


def _store(live, extra=None):
    a = {"index.json": json.dumps(live).encode()}
    a.update(extra or {})
    return a


OLD_MT = _entry("meshtastic", SHA_A, b"artifact-A")
NEW_MT = _entry("meshtastic", SHA_B, b"artifact-B", lhpc="f" * 40)
OLD_MC = _entry("meshcom", SHA_A, b"artifact-A")
NEW_MC = _entry("meshcom", SHA_C, b"artifact-C", lhpc="f" * 40)
DAEMON = _entry("daemon", SHA_A, b"artifact-A")

ARTIFACTS = {OLD_MT["filename"]: b"artifact-A", NEW_MT["filename"]: b"artifact-B",
             OLD_MC["filename"]: b"artifact-A", NEW_MC["filename"]: b"artifact-C",
             DAEMON["filename"]: b"artifact-A"}


def test_two_stacks_published_then_proof_failed_both_go_back():
    """The whole point: two candidate publishes, the proof fails, both entries return and the
    stack nobody touched is byte-identical."""
    snapshot = _index({"meshtastic": OLD_MT, "meshcom": OLD_MC, "daemon": DAEMON})
    live = _index({"meshtastic": NEW_MT, "meshcom": NEW_MC, "daemon": DAEMON})
    rel = FakeRelease(_store(live, ARTIFACTS))
    rollback(["meshtastic", "meshcom"], snapshot, {"meshtastic": NEW_MT, "meshcom": NEW_MC}, rel)
    back = json.loads(rel.assets["index.json"])["stacks"]
    assert canon(back["meshtastic"]) == canon(OLD_MT)
    assert canon(back["meshcom"]) == canon(OLD_MC)
    assert canon(back["daemon"]) == canon(DAEMON)
    assert rel.uploaded == ["SHA256SUMS", "index.json"]      # pointer switch LAST


def test_a_foreign_publish_is_a_conflict_not_a_rollback():
    """Someone published in between: the live entry is not the one this attempt produced, so
    replacing it would delete their work. Refuse, upload nothing."""
    snapshot = _index({"meshtastic": OLD_MT})
    foreign = _entry("meshtastic", SHA_C, b"artifact-C", lhpc="9" * 40)
    rel = FakeRelease(_store(_index({"meshtastic": foreign}), ARTIFACTS))
    with pytest.raises(SystemExit) as exc:
        rollback(["meshtastic"], snapshot, {"meshtastic": NEW_MT}, rel)
    assert "not the one this attempt published" in str(exc.value)
    assert rel.uploaded == []


def test_already_the_snapshot_entry_is_a_verified_no_op():
    snapshot = _index({"meshtastic": OLD_MT})
    rel = FakeRelease(_store(_index({"meshtastic": OLD_MT}), ARTIFACTS))
    report = rollback(["meshtastic"], snapshot, {"meshtastic": OLD_MT}, rel)
    assert any("verified no-op" in line for line in report)
    assert rel.uploaded == []


def test_a_missing_artifact_cannot_be_restored():
    snapshot = _index({"meshtastic": OLD_MT})
    artifacts = {k: v for k, v in ARTIFACTS.items() if k != OLD_MT["filename"]}
    rel = FakeRelease(_store(_index({"meshtastic": NEW_MT}), artifacts))
    with pytest.raises(SystemExit) as exc:
        rollback(["meshtastic"], snapshot, {"meshtastic": NEW_MT}, rel)
    assert "no longer published" in str(exc.value)
    assert rel.uploaded == []


def test_an_artifact_whose_bytes_changed_is_refused():
    snapshot = _index({"meshtastic": OLD_MT})
    artifacts = dict(ARTIFACTS, **{OLD_MT["filename"]: b"tampered"})
    rel = FakeRelease(_store(_index({"meshtastic": NEW_MT}), artifacts))
    with pytest.raises(SystemExit) as exc:
        rollback(["meshtastic"], snapshot, {"meshtastic": NEW_MT}, rel)
    assert "does not match the snapshot" in str(exc.value)
    assert rel.uploaded == []


def test_an_upload_that_did_not_take_effect_is_not_confirmed():
    """A rollback that cannot READ BACK what it wrote must not report success — the caller
    would delete its recovery state over a pointer that never moved."""
    snapshot = _index({"meshtastic": OLD_MT})
    rel = FakeRelease(_store(_index({"meshtastic": NEW_MT}), ARTIFACTS), frozen=True)
    with pytest.raises(SystemExit) as exc:
        rollback(["meshtastic"], snapshot, {"meshtastic": NEW_MT}, rel)
    assert "NOT confirmed" in str(exc.value)


def test_a_snapshot_without_the_stack_is_refused():
    rel = FakeRelease(_store(_index({"meshtastic": NEW_MT}), ARTIFACTS))
    with pytest.raises(SystemExit) as exc:
        rollback(["meshtastic"], _index({"daemon": DAEMON}), {}, rel)
    assert "no entry to restore" in str(exc.value)
    assert rel.uploaded == []


def test_a_corrupt_live_index_stops_everything():
    snapshot = _index({"meshtastic": OLD_MT})
    rel = FakeRelease({"index.json": b'{"schema": 1, "stacks": {}}'})
    with pytest.raises(SystemExit) as exc:
        rollback(["meshtastic"], snapshot, {}, rel)
    assert "the live index" in str(exc.value)
    assert rel.uploaded == []


def test_restoring_without_expect_still_validates_the_artifact():
    """No EXPECT_JSON is allowed (a by-hand undo of the last publish), but every other guard
    still applies."""
    snapshot = _index({"meshtastic": OLD_MT})
    rel = FakeRelease(_store(_index({"meshtastic": NEW_MT}), ARTIFACTS))
    rollback(["meshtastic"], snapshot, {}, rel)
    assert canon(json.loads(rel.assets["index.json"])["stacks"]["meshtastic"]) == canon(OLD_MT)
