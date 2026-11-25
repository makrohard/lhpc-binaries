"""publish.sh's artifact validation reads the decompressed stream to its end. The tar end marker ends
the member loop, not the stream: tar pads its last record with zero bytes after it. Closing zstd's
pipe at the marker could cut zstd off mid-write (EPIPE: a nonzero exit, "zstd decompression failed",
on an intact archive); with more padding than a pipe holds, it always did. What follows the marker
must be zero bytes."""
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "builders" / "publish.sh"
BIG_PAD = 1 << 20          # far more than a pipe buffer (64 KiB)


def _validator():
    """The validation step's own Python, as publish.sh runs it."""
    s = PUBLISH.read_text()
    m = re.search(r'python3 - "\$FRAG" "\$OUT" <<\'PY\'\n(.*?)\nPY\n', s, re.S)
    assert m, "publish.sh's validation heredoc moved"
    return m.group(1)


def _artifact(tmp, tail, zeros=0, poke=None):
    """A tar as tarfile writes it, then `tail`, then `zeros` zero bytes streamed into zstd; `poke` puts
    an "x" that many bytes before the tar's own end (inside its last record's padding)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        data = b"#!/bin/sh\n"
        ti = tarfile.TarInfo("src/loraham-daemon/loraham_daemon/run.sh")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    tar = bytearray(buf.getvalue())
    if poke:
        tar[-poke] = ord("x")
    raw = tmp / "artifact.tar"
    raw.write_bytes(bytes(tar) + tail)
    cmd = '{ cat "$1"; head -c "$2" /dev/zero; } | zstd -q -1 -c > "$3"'
    subprocess.run(["bash", "-c", cmd, "-", str(raw), str(zeros), str(tmp / "artifact.zst")], check=True)
    return (tmp / "artifact.zst").read_bytes()


def _validate(tmp_path, tail, **art):
    out = tmp_path / "out"
    out.mkdir()
    blob = _artifact(tmp_path, tail, **art)
    sha = hashlib.sha256(blob).hexdigest()
    (out / f"daemon-{sha}.tar.zst").write_bytes(blob)
    (out / "daemon.provenance.txt").write_text("x\n")
    comps = {"loraham-daemon": "b" * 40}
    frag = {"stack": "daemon", "filename": f"daemon-{sha}.tar.zst", "sha256": sha, "size": len(blob),
            "built_from": "f" * 40, "components": comps, "lhpc_commit": "c" * 40,
            "builder_commit": "d" * 40, "target": "aarch64-trixie", "os": "trixie",
            "container_digest": "debian@sha256:" + "e" * 64, "runtime_deps": ["libgpiod2"],
            "smoke": {"mode": "mandatory", "result": "passed"}, "extract_to": "runtime-root"}
    (out / "daemon.frag.json").write_text(json.dumps(frag))
    env = {"PATH": os.environ["PATH"], "BUILDERS_DIR": str(ROOT / "builders"), "EXPECTED_STACK": "daemon",
           "EXPECTED_BUILDER_COMMIT": "d" * 40, "EXPECTED_LHPC_COMMIT": "c" * 40,
           "EXPECTED_CONTAINER_DIGEST": "debian@sha256:" + "e" * 64, "EXPECTED_BUILT_FROM": "f" * 40,
           "EXPECTED_COMPONENTS": json.dumps(comps), "INDEX_NAME": "index.json", "RECORDING": "[]"}
    return subprocess.run(["python3", "-", str(out / "daemon.frag.json"), str(out)], input=_validator(),
                          env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)


def test_an_intact_archive_with_more_zero_padding_than_a_pipe_holds_validates(tmp_path):
    r = _validate(tmp_path, b"\0" * BIG_PAD)
    assert r.returncode == 0 and "VALIDATED daemon-" in r.stdout, (r.returncode, r.stderr)


def test_an_archive_as_tar_writes_it_validates(tmp_path):
    r = _validate(tmp_path, b"")
    assert r.returncode == 0 and "VALIDATED daemon-" in r.stdout, (r.returncode, r.stderr)


def test_data_after_the_tar_end_marker_is_refused(tmp_path):
    r = _validate(tmp_path, b"\0" * BIG_PAD + b"x")
    assert r.returncode != 0 and "data after the tar end marker" in r.stderr, (r.returncode, r.stderr)


def test_a_nonzero_byte_inside_the_last_records_padding_is_refused(tmp_path):
    # tarfile has already buffered this byte when it meets the marker: the check reads its stream
    r = _validate(tmp_path, b"", poke=100)
    assert r.returncode != 0 and "data after the tar end marker" in r.stderr, (r.returncode, r.stderr)


def test_zero_padding_counts_toward_the_expansion_limit(tmp_path):
    r = _validate(tmp_path, b"", zeros=(1 << 30) + 1)       # MAX_TOTAL_BYTES is 1 GiB
    assert r.returncode != 0 and "expands beyond" in r.stderr, (r.returncode, r.stderr)
