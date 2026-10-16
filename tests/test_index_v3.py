"""The second index (schema 3, `index-v3.json`): a per-stack history with one entry per distinct
composition, never cut by a count.

Run with: python3 -m pytest tests/
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "builders"))

from lib_index import (INDEX_V3_MAX_BYTES, index_name, inputs_map, load_index_v3,  # noqa: E402
                       merge_v3, v3_size_error, validate_entry_v3)

REPO = "o/r"


def _entry(n, pin="b" * 40, inputs=None, sid="meshtastic"):
    """A valid schema-3 entry; `n` makes the artifact (and so its filename) distinct."""
    sha = f"{n:064x}"
    e = {"filename": f"{sid}-{sha}.tar.zst",
         "url": f"https://github.com/{REPO}/releases/download/binaries/{sid}-{sha}.tar.zst",
         "sha256": sha, "size": 10, "built_from": pin, "components": {"meshtastic": pin},
         "lhpc_commit": "c" * 40, "builder_commit": "d" * 40, "target": "aarch64-trixie",
         "os": "trixie", "container_digest": "debian@sha256:" + "e" * 64,
         "runtime_deps": ["libgpiod2"], "smoke": {"mode": "mandatory", "result": "passed"},
         "extract_to": "runtime-root",
         "inputs": {"meshtastic": "f" * 64} if inputs is None else inputs,
         "published_at": "2026-10-16T08:00:00Z"}
    assert validate_entry_v3(sid, e) == "", validate_entry_v3(sid, e)
    return e


def _publish(idx, e, sid="meshtastic"):
    """One publication as publish.sh does it: merge, write, read back through the validator."""
    merged = json.loads(json.dumps(merge_v3(idx, sid, e)))
    idx, err = load_index_v3(merged)
    assert err == "", err
    return idx


def test_an_old_composition_stays_after_25_newer_publications_of_other_compositions():
    idx = _publish({"schema": 3, "stacks": {}}, _entry(0, pin="0" * 40))
    for n in range(1, 26):
        idx = _publish(idx, _entry(n, pin=f"{n:040x}"))
    hist = idx["stacks"]["meshtastic"]
    assert len(hist) == 26
    assert hist[-1] == _entry(0, pin="0" * 40)            # the oldest, unchanged, still listed
    assert [e["components"]["meshtastic"] for e in hist] == [f"{n:040x}" for n in range(25, -1, -1)]


def test_a_rebuild_of_the_same_composition_replaces_it_and_moves_to_the_front():
    idx = _publish({"schema": 3, "stacks": {}}, _entry(1, pin="1" * 40))
    idx = _publish(idx, _entry(2, pin="2" * 40))
    idx = _publish(idx, _entry(3, pin="1" * 40))           # same composition, new artifact
    assert [e["sha256"] for e in idx["stacks"]["meshtastic"]] == [f"{3:064x}", f"{2:064x}"]


def test_other_inputs_are_another_composition():
    idx = _publish({"schema": 3, "stacks": {}}, _entry(1))
    idx = _publish(idx, _entry(2, inputs={"meshtastic": "a" * 64}))
    assert len(idx["stacks"]["meshtastic"]) == 2


def test_other_stacks_are_untouched():
    idx = _publish({"schema": 3, "stacks": {}}, _entry(1, sid="daemon", inputs={}), sid="daemon")
    idx = _publish(idx, _entry(2))
    assert [e["sha256"] for e in idx["stacks"]["daemon"]] == [f"{1:064x}"]


def test_entry_rejections():
    for over, msg in [
            ({"inputs": {"other": "f" * 64}}, "bad inputs entry"),
            ({"inputs": {"meshtastic": "short"}}, "bad inputs entry"),
            ({"published_at": "2026-10-16 08:00:00"}, "published_at"),
            ({"channel": "stable"}, "unexpected field"),       # B2 adds it, B1 refuses it
            ({"smoke": {"mode": "skipped", "result": "skipped"}}, "smoke gate")]:
        e = dict(_entry(1), **over)
        assert msg in validate_entry_v3("meshtastic", e), over
    for field in ("inputs", "published_at"):
        e = _entry(1)
        del e[field]
        assert "missing" in validate_entry_v3("meshtastic", e), field


def test_published_at_is_ascii_digits_only():
    # The controller's reader takes [0-9] only; `\d` would also take Arabic-Indic digits.
    import time
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())     # what publish.sh writes
    assert validate_entry_v3("meshtastic", dict(_entry(1), published_at=stamp)) == ""
    e = dict(_entry(1), published_at="\u0662\u0660\u0662\u0666-10-16T08:00:00Z")
    assert "published_at" in validate_entry_v3("meshtastic", e)


def test_a_numeric_digest_or_pin_is_refused():
    # 10**63 prints as 64 decimal digits, which a check on str(digest) would take for hex.
    e = dict(_entry(1), inputs={"meshtastic": 10**63})
    assert "bad inputs entry" in validate_entry_v3("meshtastic", e)
    raw = {"schema": 3, "stacks": {"meshtastic": [json.loads(json.dumps(e))]}}
    assert "bad inputs entry" in load_index_v3(raw)[1]
    e = dict(_entry(1), components={"meshtastic": 10**39}, inputs={})
    assert "bad components entry" in validate_entry_v3("meshtastic", e)


def test_load_refusals():
    good = {"schema": 3, "stacks": {"meshtastic": [_entry(1)]}}
    assert load_index_v3(good)[1] == ""
    bad_entry = dict(_entry(1), size=0)
    for doc in ({"schema": 2, "stacks": {}},
                {"schema": 3, "stacks": {}, "extra": 1},
                {"schema": 3, "stacks": {"meshtastic": _entry(1)}},
                {"schema": 3, "stacks": {"meshtastic": [bad_entry]}},
                {"schema": 3},
                [1]):
        idx, err = load_index_v3(doc)
        assert idx is None and err, doc


def test_inputs_map():
    assert inputs_map([], []) == ({}, "")
    assert inputs_map(["meshtastic"], ["f" * 64]) == ({"meshtastic": "f" * 64}, "")
    assert "more than one component" in inputs_map(["a", "b"], ["f" * 64, "e" * 64])[1]
    assert "0 .lhpc-build-inputs member(s)" in inputs_map(["meshtastic"], [])[1]
    assert "2 .lhpc-build-inputs member(s)" in inputs_map(["meshtastic"], ["f" * 64] * 2)[1]


def test_index_name():
    base = f"https://github.com/{REPO}/releases/download/binaries/"
    assert index_name(base + "index.json", REPO, "binaries") == "index.json"
    assert index_name(base + "index-v3.json", REPO, "binaries") == "index-v3.json"
    for url in (base + "index.prev.json", base + "SHA256SUMS",
                "https://github.com/x/y/releases/download/binaries/index-v3.json",
                base.replace("https", "http") + "index-v3.json"):
        assert index_name(url, REPO, "binaries") is None, url


def test_another_target_is_another_composition():
    # Only aarch64-trixie validates today, so this merges directly: the key must still tell two
    # targets apart, or a second target's build would replace the first's.
    a, b = _entry(1), dict(_entry(2), target="x86_64-trixie")
    idx = merge_v3(merge_v3({"schema": 3, "stacks": {}}, "meshtastic", a), "meshtastic", b)
    assert [e["target"] for e in idx["stacks"]["meshtastic"]] == ["x86_64-trixie", "aarch64-trixie"]
    idx = merge_v3(idx, "meshtastic", _entry(3))                 # a same-target rebuild
    assert [e["sha256"][-1] for e in idx["stacks"]["meshtastic"]] == ["3", "2"]


def test_a_history_past_what_a_controller_reads_is_refused():
    assert v3_size_error("x" * INDEX_V3_MAX_BYTES) == ""
    assert "past the" in v3_size_error("x" * (INDEX_V3_MAX_BYTES + 1))


def test_inputs_come_only_from_the_components_the_artifact_covers():
    # The meshtastic stack also holds the web client (`meshtastic-web`, its own index entries), which
    # records asset inputs too; it is not in the artifact, so it is not a key of the entry's inputs.
    from types import SimpleNamespace as NS

    from lib_provenance import recording_components
    comps = [NS(id="meshtastic", build_inputs=(("cli", "2.7"),), asset_inputs=()),
             NS(id="meshtastic-web", build_inputs=(), asset_inputs=("meshtastic-web-assets.sh",)),
             NS(id="meshtastic-cli-venv", build_inputs=(), asset_inputs=())]
    st = NS(components=comps, binary=NS(covers=("meshtastic",)))
    assert recording_components(st) == ["meshtastic"]
    assert inputs_map(recording_components(st), ["f" * 64])[1] == ""
