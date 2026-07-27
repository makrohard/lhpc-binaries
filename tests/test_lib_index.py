"""The shared index validator: what may be published, and what an existing index may look like.

Run with: python3 -m pytest tests/ (or plain `python3 tests/test_lib_index.py`).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "builders"))

from lib_index import load_index, validate_entry           # noqa: E402


def _entry(sid="daemon", **over):
    sha = "a" * 64
    e = {"filename": f"{sid}-{sha}.tar.zst",
         "url": f"https://github.com/o/r/releases/download/binaries/{sid}-{sha}.tar.zst",
         "sha256": sha, "size": 10, "built_from": "x", "components": {"loraham-daemon": "b" * 40},
         "lhpc_commit": "c" * 40, "builder_commit": "d" * 40, "target": "aarch64-trixie",
         "os": "trixie", "container_digest": "debian@sha256:" + "e" * 64,
         "runtime_deps": ["libgpiod2"], "smoke": {"mode": "mandatory", "result": "passed"},
         "extract_to": "runtime-root"}
    e.update(over)
    return e


def test_valid_entry_passes():
    assert validate_entry("daemon", _entry()) == ""


def test_entry_rejections():
    for field, value, msg in [
            ("sha256", "zz", "malformed sha256"),
            ("smoke", {"mode": "skipped", "result": "skipped"}, "smoke gate"),
            ("url", "http://x/daemon.tar.zst", "non-HTTPS"),
            ("components", {}, "empty components"),
            ("builder_commit", "short", "not a full commit sha"),
            ("size", 0, "invalid size"),
            ("runtime_deps", ["evil; rm -rf /"], "suspicious runtime dep")]:
        assert msg in validate_entry("daemon", _entry(**{field: value})), field


def test_entry_cannot_be_filed_under_the_wrong_stack():
    assert "content-addressed" in validate_entry("meshcom", _entry("daemon"))


def test_url_must_point_at_its_own_artifact():
    assert "does not point at its own artifact" in validate_entry(
        "daemon", _entry(url="https://github.com/o/r/releases/download/binaries/other.tar.zst"))


# --- load_index: ONLY a schema-2 index is accepted -----------------------------------------

def test_schema_2_index_is_accepted():
    idx, err = load_index({"schema": 2, "stacks": {}})
    assert err == "" and idx["stacks"] == {}


def test_empty_object_is_not_an_empty_index():
    """A present-but-empty index.json is NOT the same as a missing asset (the caller handles
    that separately) — treating it as empty would erase every preserved stack."""
    assert load_index({})[0] is None


def test_a_v1_shaped_document_is_refused():
    """The old "looks like v1" rule accepted this and replaced the index with an empty one."""
    idx, err = load_index({"daemon": {"filename": "broken", "sha256": "broken"}})
    assert idx is None and "unknown index schema" in err


def test_unknown_schema_is_refused():
    assert load_index({"schema": 3, "stacks": {}})[0] is None


def test_missing_stacks_table_is_refused():
    assert load_index({"schema": 2})[0] is None


def test_non_object_is_refused():
    assert load_index([1, 2])[0] is None


if __name__ == "__main__":                                  # no pytest needed in CI containers
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                fails += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if fails else 0)
