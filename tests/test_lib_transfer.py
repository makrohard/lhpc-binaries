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
    # A status decides before any transport word: here the URL holds one.
    "fatal: unable to access 'https://example.org/ReadTimeoutError/repo.git/': The requested URL "
    "returned error: 404\n",
    "PackageException: Got the unrecognized status code '404' when downloaded "
    "https://example.org/timed out/x.zip\n",
    "fatal: unable to access 'https://github.com/x/y.git/': The requested URL returned error: 503\n"
    "ERROR: meson setup failed\nERROR: Git command failed\nERROR: meson setup failed\n",
    "A wrapper over an error of its own:\nerror: patch does not apply\nERROR: meson setup failed\n",
    "Compiling ... done\n",
    "",
], ids=["recovered-then-compile-error", "http-404", "http-404-transport-word-in-url",
        "platformio-404-transport-word-in-url", "three-wrappers-deep", "wrapper-over-own-error",
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


def test_the_whole_step_log_is_read_however_long_its_blank_tail(tmp_path):
    """No tail window: the 504 that ended the step decides behind 30 000 blank lines."""
    log = tmp_path / "build-meshtastic-1.log"
    log.write_text(PIO_504 + "\n" * 30000)
    out = f"  [failed] build meshtastic (rc 1, log {log})\n"
    assert "status code '504'" in lt.decisive_transfer(lt.step_text(out))


def test_the_last_typed_failure_names_the_log_that_decides(tmp_path):
    first, last = tmp_path / "build-a-1.log", tmp_path / "build-b-1.log"
    first.write_text("src/a.c:1:1: error: boom\n")
    last.write_text(PIO_504)
    out = (f"  [failed] build a (rc 1, log {first})\n  [failed] build b (rc 1, log {last})\n")
    assert "status code '504'" in lt.decisive_transfer(lt.step_text(out))


RECOVERED = "curl: (56) Recv failure: Connection reset by peer\nretrying ... ok\n"


@pytest.mark.parametrize("ending", [
    "src/main.cpp:3:10: fatal error: RadioLib.h: No such file or directory\n",
    "collect2: error: ld returned 1 exit status\n",
    "main.cpp:(.text+0x1c): undefined reference to `radio_init'\n",
    "make: *** [Makefile:12: all] Error 2\n",
    "*** [.pio/build/heltec/firmware.elf] Error 1\n",
    "FAILED: src/CMakeFiles/app.dir/main.c.o\nninja: build stopped: subcommand failed.\n",
    "CMake Error at CMakeLists.txt:10 (find_package):\n  Could not find a package\n",
    "Something this rule has never seen\n",
], ids=["fatal-error", "collect2", "undefined-reference", "make", "scons-platformio", "ninja",
        "cmake", "unknown-trailer"])
def test_a_transfer_followed_by_any_other_ending_is_not_repeated(ending):
    """Default deny: after the transfer line only blank lines, the wrappers and meson's log pointer
    may come; any other ending decides, and it is no transfer."""
    assert lt.decisive_transfer(RECOVERED + ending) == ""
    assert lt.decisive_transfer(RECOVERED.splitlines()[0]) != "", "alone, that line is a transfer"


@pytest.mark.parametrize("line", [
    "src/WebServer.cpp:88:13: error: 'ConnectionError' was not declared in this scope\n",
    "src/net.c:4:10: fatal error: timed out.h: No such file or directory\n",
], ids=["error", "fatal-error"])
def test_a_compiler_diagnostic_is_never_a_transfer_whatever_it_names(line):
    assert lt.decisive_transfer(line) == ""


def test_a_meson_download_failure_with_a_5xx_is_one():
    line = ("../qemu/meson.build:12:0: ERROR: Download failed: HTTP Error 503: Service Unavailable"
            " (https://wrapdb.mesonbuild.com/v2/zlib/get_patch)\n")
    assert "HTTP Error 503" in lt.decisive_transfer(line)
