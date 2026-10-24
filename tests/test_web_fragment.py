"""The builder's own fragment (lib_web.web_fragment, what meshtastic-web.sh writes): a pinned build
carries exactly `"diagnostic": true`; an unpinned one has no such key and stays a valid index entry,
while the marked one is refused by the index validator too."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "builders"))

from lib_index import validate_entry  # noqa: E402
from lib_web import web_fragment  # noqa: E402

ENV = {"FNAME": "meshtastic-web-" + "a" * 64 + ".tar.zst", "SHA": "a" * 64, "WEB_COMMIT": "b" * 40,
       "MTD_PIN": "c" * 40, "LHPC_COMMIT": "d" * 40, "BUILDER_COMMIT": "e" * 40,
       "CONTAINER_DIGEST": "debian@sha256:" + "f" * 64, "CHANNEL": "dev",
       "WEB_VERSION": "nightly-v2.7.2-bbbbbbbbbbbb"}


def _entry(frag):
    e = {k: v for k, v in frag.items() if k not in ("stack", "channel", "web_version")}
    e["url"] = "https://example.invalid/" + frag["filename"]
    return e


def test_a_pinned_build_carries_exactly_the_diagnostic_marker():
    frag = web_fragment({**ENV, "SOURCE_COMMIT": "b" * 40}, 123)
    assert frag["diagnostic"] is True
    assert set(frag) - set(web_fragment(ENV, 123)) == {"diagnostic"}
    assert validate_entry("meshtastic-web", _entry(frag))                 # an error: never an index entry


def test_an_unpinned_build_has_no_marker_and_stays_a_valid_entry():
    for env in (ENV, {**ENV, "SOURCE_COMMIT": ""}):
        frag = web_fragment(env, 123)
        assert "diagnostic" not in frag
        assert validate_entry("meshtastic-web", _entry(frag)) == ""      # valid
