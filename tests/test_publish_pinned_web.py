"""publish.sh refuses a meshtastic-web artifact built from a pinned upstream commit (exit 4) before it
fetches anything; another stack with a source_commit gets past that check."""
import os
import subprocess
from pathlib import Path

PUBLISH = Path(__file__).resolve().parents[1] / "builders" / "publish.sh"


def _publish(tmp_path, stack, source_commit, frag="{}"):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / f"{stack}.frag.json").write_text(frag)
    env = {"PATH": os.environ["PATH"], "GITHUB_REPOSITORY": "o/r", "EXPECTED_STACK": stack,
           "EXPECTED_BUILDER_COMMIT": "b" * 40, "EXPECTED_LHPC_COMMIT": "not-a-sha",
           "EXPECTED_CONTAINER_DIGEST": "d", "SOURCE_COMMIT": source_commit}
    return subprocess.run(["bash", str(PUBLISH), str(tmp_path / "out")], env=env,
                          capture_output=True, text=True, timeout=30)


def test_a_pinned_web_build_is_refused_before_anything_is_fetched(tmp_path):
    out = _publish(tmp_path, "meshtastic-web", "a" * 40)
    assert out.returncode == 4 and "takes no source_commit" in out.stderr


def test_another_stack_with_a_source_commit_passes_that_check(tmp_path):
    # It stops at the NEXT check (the lhpc commit is not a sha), never at the web refusal.
    out = _publish(tmp_path, "daemon", "a" * 40)
    assert out.returncode == 4 and "EXPECTED_LHPC_COMMIT is not a 40-hex sha" in out.stderr


def test_a_fragment_marked_diagnostic_is_refused_with_the_env_empty(tmp_path):
    """round web-2: the refusal must not rest on the env alone. A pinned build marks its fragment
    ("diagnostic", any value); with SOURCE_COMMIT empty the marker alone refuses it, before anything
    is fetched."""
    out = _publish(tmp_path, "meshtastic-web", "", frag='{"stack": "meshtastic-web", "diagnostic": true}')
    assert out.returncode == 4 and "is a diagnostic build" in out.stderr


def test_an_unmarked_fragment_passes_the_marker_check(tmp_path):
    out = _publish(tmp_path, "meshtastic-web", "", frag='{"stack": "meshtastic-web"}')
    assert out.returncode == 4 and "EXPECTED_LHPC_COMMIT is not a 40-hex sha" in out.stderr
