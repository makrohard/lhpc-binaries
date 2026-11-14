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

import lib_index  # noqa: E402
import rollback as rollback_mod  # noqa: E402
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


# ---- mode withdraw-v3: the release bot's own unproven entries leave index-v3.json ----
# The bot names each entry by its exact snapshot key (`entry_key`). An entry of the same file and
# sha256 under another key is another publisher's (its `build_run` differs), so it is never removed:
# the run fails and nothing is uploaded. The keys are the bot's own (tests/entry_key_vectors.json,
# the same file in lhpc-release-bot).
HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "entry_key_vectors.json")) as _fh:
    VECTORS = json.load(_fh)["vectors"]
# Vectors 0-2: one meshtastic artifact as published by no run (legacy), run attempt 1, attempt 2.
LEGACY_V3, MINE_V3, OTHER_V3 = (VECTORS[i]["entry"] for i in range(3))
DAEMON_V3, WEB_V3 = VECTORS[3]["entry"], VECTORS[4]["entry"]


def vkey(i):
    return VECTORS[i]["key"]


class FakeReleaseV3:
    """The rolling release holding index-v3.json; `frozen` = an upload that does not take."""

    def __init__(self, stacks, frozen=False):
        self.assets = {} if stacks is None else {
            "index-v3.json": json.dumps({"schema": 3, "stacks": stacks}).encode()}
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

    def stacks(self):
        return json.loads(self.assets["index-v3.json"])["stacks"]


def _run(monkeypatch, release, keys, stacks=None, mode="withdraw-v3"):
    """rollback.py's main() with the environment rollback.yml sets."""
    monkeypatch.setattr(rollback_mod, "Release", lambda repo, tag: release)
    for k, v in {"GITHUB_REPOSITORY": "o/r", "MODE": mode,
                 "KEYS_JSON": keys if isinstance(keys, str) else json.dumps(keys),
                 "STACKS": ",".join(sorted(keys)) if stacks is None else stacks}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SNAPSHOT_JSON", raising=False)
    monkeypatch.delenv("EXPECT_JSON", raising=False)
    return rollback_mod.main()


def test_the_shared_vectors_are_valid_entries_with_the_bots_keys():
    for v in VECTORS:
        assert lib_index.validate_entry_v3(v["stack"], v["entry"]) == ""
        assert lib_index.entry_key(v["entry"]) == v["key"], v["stack"]
    assert len({tuple(v["key"]) for v in VECTORS}) == len(VECTORS)
    assert vkey(1)[:2] == vkey(2)[:2] and vkey(1)[2] != vkey(2)[2]     # attempts differ in fp only
    assert vkey(1)[3] == vkey(2)[3] == vkey(0)[3]                      # one composition


def test_an_exact_key_is_removed_and_read_back(monkeypatch, capsys):
    rel = FakeReleaseV3({"meshtastic": [MINE_V3], "daemon": [DAEMON_V3],
                         "meshtastic-web": [WEB_V3]})
    assert _run(monkeypatch, rel, {"meshtastic": [vkey(1)]}) == 0
    assert rel.uploaded == ["index-v3.json"]
    assert rel.stacks() == {"meshtastic": [], "daemon": [DAEMON_V3], "meshtastic-web": [WEB_V3]}
    assert "read back" in capsys.readouterr().out


def test_only_the_named_entry_of_a_history_goes(monkeypatch):
    older = dict(DAEMON_V3, filename=DAEMON_V3["filename"].replace("2.tar", "4.tar"),
                 sha256=DAEMON_V3["sha256"][:-1] + "4", components={"daemon": "9" * 40},
                 url=DAEMON_V3["url"].replace("2.tar", "4.tar"))
    assert lib_index.validate_entry_v3("daemon", older) == ""
    rel = FakeReleaseV3({"daemon": [DAEMON_V3, older]})
    assert _run(monkeypatch, rel, {"daemon": [vkey(3)]}) == 0
    assert rel.stacks() == {"daemon": [older]}


def test_the_same_file_from_another_run_is_never_withdrawn(monkeypatch):
    """Our key names attempt 1; attempt 2 of the same run (or another run) published the same
    artifact: that entry is not ours, so the withdrawal fails and nothing is uploaded."""
    rel = FakeReleaseV3({"meshtastic": [OTHER_V3], "daemon": [DAEMON_V3]})
    with pytest.raises(SystemExit, match="other provenance"):
        _run(monkeypatch, rel, {"daemon": [vkey(3)], "meshtastic": [vkey(1)]})
    assert rel.uploaded == []
    assert rel.stacks() == {"meshtastic": [OTHER_V3], "daemon": [DAEMON_V3]}


def test_a_legacy_entry_of_the_same_file_is_never_withdrawn(monkeypatch):
    rel = FakeReleaseV3({"meshtastic": [LEGACY_V3]})
    with pytest.raises(SystemExit, match="other provenance"):
        _run(monkeypatch, rel, {"meshtastic": [vkey(1)]})
    assert rel.uploaded == []


def test_an_absent_key_is_already_withdrawn(monkeypatch, capsys):
    rel = FakeReleaseV3({"meshtastic": [], "daemon": [DAEMON_V3]})
    assert _run(monkeypatch, rel, {"meshtastic": [vkey(1)]}) == 0
    assert rel.uploaded == []
    assert "already withdrawn" in capsys.readouterr().out


def test_a_withdrawal_whose_upload_did_not_take_effect_is_not_confirmed(monkeypatch):
    rel = FakeReleaseV3({"meshtastic": [MINE_V3]}, frozen=True)
    with pytest.raises(SystemExit, match="NOT confirmed"):
        _run(monkeypatch, rel, {"meshtastic": [vkey(1)]})
    assert rel.uploaded == ["index-v3.json"]


def test_a_release_without_index_v3_fails(monkeypatch):
    rel = FakeReleaseV3(None)
    with pytest.raises(SystemExit, match="no index-v3.json"):
        _run(monkeypatch, rel, {"meshtastic": [vkey(1)]})
    assert rel.uploaded == []


@pytest.mark.parametrize("keys, stacks", [
    ("not json", "meshtastic"),
    ('["x"]', "meshtastic"),
    ({"meshtastic": []}, None),
    ({"meshtastic": [["a", "b", "c"]]}, None),
    ({"meshtastic": [["a", "b", "c", 4]]}, None),
    ({"meshtastic": [vkey(1)]}, "meshtastic,daemon"),          # STACKS names another stack
])
def test_malformed_keys_refuse_before_any_read(monkeypatch, capsys, keys, stacks):
    rel = FakeReleaseV3({"meshtastic": [MINE_V3]})
    assert _run(monkeypatch, rel, keys, stacks=stacks) == 2
    assert "KEYS_JSON must be" in capsys.readouterr().err
    assert rel.uploaded == [] and rel.stacks() == {"meshtastic": [MINE_V3]}


def test_an_unknown_mode_is_refused(monkeypatch, capsys):
    rel = FakeReleaseV3({"meshtastic": [MINE_V3]})
    assert _run(monkeypatch, rel, {"meshtastic": [vkey(1)]}, mode="withdraw") == 2
    assert "unknown MODE 'withdraw'" in capsys.readouterr().err
    assert rel.uploaded == []


def test_a_blank_mode_is_the_restore_of_index_json(monkeypatch, capsys):
    """The workflow's default and every older caller: the schema-2 restore, unchanged."""
    rel = FakeReleaseV3({"meshtastic": [MINE_V3]})
    assert _run(monkeypatch, rel, {"meshtastic": [vkey(1)]}, mode="") == 2
    assert "no index.prev.json" in capsys.readouterr().err
    assert rel.uploaded == []


def test_the_workflow_passes_mode_and_keys_to_the_script():
    with open(os.path.join(os.path.dirname(HERE), ".github", "workflows", "rollback.yml")) as fh:
        text = fh.read()
    inputs = text.split("inputs:", 1)[1].split("permissions:", 1)[0]
    assert "\n      mode:\n" in inputs and "\n      keys_json:\n" in inputs
    assert "MODE: ${{ github.event.inputs.mode }}" in text
    assert "KEYS_JSON: ${{ github.event.inputs.keys_json }}" in text
