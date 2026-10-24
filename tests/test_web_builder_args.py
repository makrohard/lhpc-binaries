"""`meshtastic-web.sh` takes a `source_commit` only as a dev-channel diagnostic: a full 40-hex
upstream commit, refused on stable and in any other shape. Both refusals happen before the
builder touches the network."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

BUILDER = Path(__file__).resolve().parents[1] / "builders" / "meshtastic-web.sh"
SHA = "53ffb84b3c82e3d3f8d6451d9c495a1459875bfa"


def _run(channel, commit):
    env = {"PATH": os.environ["PATH"], "WEB_CHANNEL": channel, "SOURCE_COMMIT": commit}
    return subprocess.run(["bash", str(BUILDER)], env=env, capture_output=True, text=True,
                          timeout=30)


def test_a_stable_build_refuses_a_source_commit():
    out = _run("stable", SHA)
    assert out.returncode == 2
    assert "only on channel dev" in out.stderr


@pytest.mark.parametrize("commit", ["main", SHA[:12], SHA.upper(), SHA + "0", f"{SHA} "])
def test_a_dev_source_commit_must_be_a_full_lowercase_sha(commit):
    out = _run("dev", commit)
    assert out.returncode == 2
    assert "must be a full 40-hex upstream commit" in out.stderr
