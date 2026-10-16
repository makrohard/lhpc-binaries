"""The meshtastic-web builder's pure parts: what a web entry in index-v3.json may look like, what its
artifact may contain, and the helpers the build and the lab smoke rely on.

Run with: python3 -m pytest tests/
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "builders"))

from lib_index import load_index_v3, merge_v3, path_allowed, validate_entry_v3  # noqa: E402
from lib_web import main_bundle, peeled_commit, pick_daemon_entry  # noqa: E402
from smoke_web import long_name  # noqa: E402

WEB, MTD = "a" * 40, "b" * 40


def _web(n=1, channel="dev", web=WEB, mtd=MTD, **over):
    sha = f"{n:064x}"
    e = {"filename": f"meshtastic-web-{sha}.tar.zst",
         "url": f"https://github.com/o/r/releases/download/binaries/meshtastic-web-{sha}.tar.zst",
         "sha256": sha, "size": 10, "built_from": web,
         "components": {"meshtastic-web": web, "meshtastic": mtd},
         "lhpc_commit": "c" * 40, "builder_commit": "d" * 40, "target": "aarch64-trixie",
         "os": "trixie", "container_digest": "debian@sha256:" + "e" * 64, "runtime_deps": [],
         "smoke": {"mode": "mandatory", "result": "passed"}, "extract_to": "runtime-root",
         "inputs": {}, "published_at": "2026-10-16T08:00:00Z",
         "channel": channel, "web_version": "v2.7.2" if channel == "stable" else "nightly-v2.7.2-" + web[:12]}
    e.update(over)
    return e


def _daemon(n, pin, sid="meshtastic"):
    sha = f"{n:064x}"
    return {"filename": f"{sid}-{sha}.tar.zst",
            "url": f"https://github.com/o/r/releases/download/binaries/{sid}-{sha}.tar.zst",
            "sha256": sha, "size": 10, "built_from": pin, "components": {"meshtastic": pin},
            "lhpc_commit": "c" * 40, "builder_commit": "d" * 40, "target": "aarch64-trixie",
            "os": "trixie", "container_digest": "debian@sha256:" + "e" * 64,
            "runtime_deps": ["libgpiod3"], "smoke": {"mode": "mandatory", "result": "passed"},
            "extract_to": "runtime-root", "inputs": {"meshtastic": "f" * 64},
            "published_at": "2026-10-16T08:00:00Z"}


def test_a_stable_and_a_dev_entry_are_valid():
    for ch in ("stable", "dev"):
        assert validate_entry_v3("meshtastic-web", _web(channel=ch)) == ""


def test_web_entry_rejections():
    for over, msg in [
            ({"channel": "nightly"}, "channel"),
            ({"web_version": ""}, "web_version"),
            ({"inputs": {"meshtastic": "f" * 64}}, "inputs"),
            ({"components": {"meshtastic-web": WEB}}, "components"),
            ({"components": {"meshtastic-web": WEB, "meshtastic": MTD, "x": MTD}}, "components"),
            ({"runtime_deps": ["libfoo1"]}, "runtime_deps")]:
        assert msg in validate_entry_v3("meshtastic-web", _web(**over)), over
    for field in ("channel", "web_version"):
        e = _web()
        del e[field]
        assert field in validate_entry_v3("meshtastic-web", e), field


def test_a_web_entry_without_inputs_is_refused_as_missing():
    # The web checks read `inputs`; the required-field check must come first.
    for field in ("inputs", "published_at"):
        e = _web()
        del e[field]
        assert f"field {field!r} missing" in validate_entry_v3("meshtastic-web", e), field


def test_only_web_entries_carry_a_channel():
    e = dict(_daemon(1, MTD), channel="dev", web_version="x")
    assert "unexpected field" in validate_entry_v3("meshtastic", e)


def test_the_web_artifact_holds_only_the_served_tree():
    assert path_allowed("build/tools/meshtastic-web/web/index.html", False, "meshtastic-web")
    assert path_allowed("build/tools/meshtastic-web/web/assets/x.js", False, "meshtastic-web")
    for isdir, name in ((True, "build"), (True, "build/tools"), (True, "build/tools/meshtastic-web")):
        assert path_allowed(name, isdir, "meshtastic-web"), name
    # The controller's own files sit beside the served root; an artifact must never carry them.
    for name in ("build/tools/meshtastic-web/.lhpc-built", "build/tools/meshtastic-web/web.provenance",
                 "build/tools/meshtasticd/web/index.html"):
        assert not path_allowed(name, False, "meshtastic-web"), name


def test_stable_and_dev_of_the_same_commit_are_two_entries():
    idx = {"schema": 3, "stacks": {}}
    for e in (_web(1, "stable"), _web(2, "dev"), _web(3, "dev")):   # the last is a dev rebuild
        idx, err = load_index_v3(json.loads(json.dumps(merge_v3(idx, "meshtastic-web", e))))
        assert err == "", err
    assert [(e["channel"], e["sha256"][-1]) for e in idx["stacks"]["meshtastic-web"]] == [
        ("dev", "3"), ("stable", "1")]


def test_pick_daemon_entry_takes_the_first_with_the_pin():
    idx = {"schema": 3, "stacks": {"meshtastic": [_daemon(1, "1" * 40), _daemon(2, MTD),
                                                  _daemon(3, MTD)]}}
    assert pick_daemon_entry(idx, MTD)["sha256"] == f"{2:064x}"
    assert pick_daemon_entry(idx, "9" * 40) is None
    assert pick_daemon_entry({"schema": 3, "stacks": {}}, MTD) is None


def test_peeled_commit_prefers_the_annotated_tags_target():
    out = (f"{'1' * 40}\trefs/tags/v2.7.2\n"
           f"{'2' * 40}\trefs/tags/v2.7.2^{{}}\n")
    assert peeled_commit(out, "v2.7.2") == "2" * 40
    assert peeled_commit(f"{'3' * 40}\trefs/tags/v2.7.1\n", "v2.7.1") == "3" * 40   # lightweight
    assert peeled_commit("", "v2.7.2") is None
    assert peeled_commit(f"{'3' * 40}\trefs/tags/v2.7.10\n", "v2.7.1") is None


def test_main_bundle_is_the_module_script_index_html_loads():
    html = ('<html><head><link rel="stylesheet" href="/index-Cx5yeQ40.css">'
            '<script type="module" crossorigin src="/index-BTHwHyj7.js"></script></head></html>')
    assert main_bundle(html) == "index-BTHwHyj7.js"
    assert main_bundle("<html></html>") is None


def test_long_name_comes_from_the_daemons_owner_line():
    log = ("INFO  | 07:07:21 0.025 Webserver started\n"
           "INFO  | 07:07:51 30.006 [NodeInfo] Send owner !7e595920/Meshtastic f640/f640\n")
    assert long_name(log) == "Meshtastic f640"
    assert long_name("INFO  | 07:07:21 0.025 Webserver started\n") is None
