"""Which failed `lhpc build` was ENDED by a transfer, and so may be repeated by `build_stack`.

Each case is a shape a build really ended with, or the one the rule must never repeat: a transfer
the build recovered from, followed by a failure of its own.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "lib_transfer", Path(__file__).resolve().parents[1] / "builders" / "lib_transfer.py")
lt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lt)

# build-binary 34133564060 (meshtastic, 2026-09-07): a PlatformIO library download.
PIO_504 = """\
Library Manager: TBPubSubClient@2.12.1 has been installed!
Library Manager: Installing https://github.com/arduino-libraries/NTPClient/archive/refs/tags/3.2.1.zip
PackageException: Got the unrecognized status code '504' when downloaded https://github.com/arduino-libraries/NTPClient/archive/refs/tags/3.2.1.zip
"""
# build-binary 36070840336 (meshcom, 2026-09-24): a meson subproject fetch inside the qemu build.
MESON_503 = """\
Initialized empty Git repository in /build/root/build/tool-cache/qemu-xtensa/qemu/subprojects/keycodemapdb/.git/
fatal: unable to access 'https://gitlab.com/qemu-project/keycodemapdb.git/': The requested URL returned error: 503

../qemu/ui/meson.build:175:22: ERROR: Git command failed: ['/usr/bin/git', 'fetch', '--depth', '1', 'origin', 'f5772a62ec52591ff6870b7e8ef32482371f22c6']

A full log can be found at /build/root/build/tool-cache/qemu-xtensa/build/meson-logs/meson-log.txt

ERROR: meson setup failed
"""


@pytest.mark.parametrize("text, decided", [
    (PIO_504, "status code '504'"),
    (MESON_503, "returned error: 503"),
    ("curl: (6) Could not resolve host: github.com\n", "Could not resolve host"),
], ids=["platformio-504", "meson-git-503", "curl-dns"])
def test_a_build_ended_by_a_transfer_is_one(text, decided):
    assert decided in lt.decisive_transfer(text)


@pytest.mark.parametrize("text", [
    # The round-1 counterexample: a transfer the build recovered from, then its own failure.
    "curl: (28) Operation timed out after 30001 milliseconds\nretrying ... ok\n"
    "src/main.cpp:12:5: error: 'radio' was not declared in this scope\n",
    "fatal: unable to access 'https://github.com/x/y.git/': The requested URL returned error: 404\n",
    "fatal: unable to access 'https://github.com/x/y.git/': The requested URL returned error: 503\n"
    "ERROR: meson setup failed\nERROR: Git command failed\nERROR: meson setup failed\n",
    "A wrapper over an error of its own:\nerror: patch does not apply\nERROR: meson setup failed\n",
    "Compiling ... done\n",
    "",
], ids=["recovered-then-compile-error", "http-404", "three-wrappers-deep", "wrapper-over-own-error",
        "no-error-line", "empty"])
def test_anything_else_is_not(text):
    assert lt.decisive_transfer(text) == ""


def test_the_failing_steps_own_log_is_read_not_the_summary(tmp_path):
    log = tmp_path / "build-meshcom-qemu-1.log"
    log.write_text(MESON_503)
    out = (f"  [ok] build meshcom-bridge\n  [failed] build meshcom-qemu (rc 1, log {log})\n"
           "ERR   build failed\n")
    assert "returned error: 503" in lt.decisive_transfer(lt.step_text(out))


def test_without_a_readable_step_log_the_output_decides_without_the_summary_line(tmp_path):
    out = (f"{PIO_504}  [failed] build meshtastic (rc 1, log {tmp_path / 'gone.log'})\n")
    assert "status code '504'" in lt.decisive_transfer(lt.step_text(out))


def test_the_command_prints_the_deciding_line_only_for_a_transfer(tmp_path, capsys):
    cap = tmp_path / "cap"
    cap.write_text(PIO_504)
    assert lt.main([str(cap)]) == 0 and "504" in capsys.readouterr().out
    cap.write_text("error: 'radio' was not declared in this scope\n")
    assert lt.main([str(cap)]) == 0 and capsys.readouterr().out == ""
