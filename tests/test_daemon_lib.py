"""The daemon builder's Meshstick library step. `read_src --optional` says whether this lhpc declares
the pine64 source (and still fails when the manifest does not load); `daemon_lib` accepts the
library only where the daemon's build.sh writes it and only with the pinned commit embedded. Both
run through the real `lib-build.sh`, against a stand-in manifest loader."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "builders" / "lib-build.sh"
PIN = "b0694ec84091b293cb1e97e00a821ff88353274f"
REMOTE = "https://github.com/pine64/libch341-spi-userspace"

_MANIFEST = '''
from types import SimpleNamespace as N

def _c(cid, remote, pin, path):
    return N(id=cid, source=N(remote=remote, pin_commit=pin, path=path))

def load_manifest():
    if {broken}:
        raise RuntimeError("manifest does not load")
    comps = [_c("loraham-daemon", "https://example.invalid/d", "1" * 40, "src/loraham-daemon")]
    if {declared}:
        comps.append(_c("libch341", "{remote}", "{pin}", "src/libch341-spi-userspace"))
    return [N(id="daemon", components=comps)]
'''


def _lhpc(tmp_path, *, declared=True, broken=False):
    core = tmp_path / "py" / "lhpc" / "core"
    core.mkdir(parents=True)
    (core.parent / "__init__.py").write_text("")
    (core / "__init__.py").write_text("")
    (core / "manifest.py").write_text(_MANIFEST.format(declared=declared, broken=broken,
                                                       remote=REMOTE, pin=PIN))
    return tmp_path / "py"


def _bash(script, pythonpath=None):
    env = {"PATH": os.environ["PATH"], "PY": sys.executable}
    if pythonpath:
        env["PYTHONPATH"] = str(pythonpath)
    return subprocess.run(["bash", "-c", f'set -euo pipefail; source "{LIB}"; {script}'],
                          env=env, capture_output=True, text=True, timeout=30)


def test_a_declared_pine64_source_is_read_with_its_pin(tmp_path):
    out = _bash("read_src --optional libch341", _lhpc(tmp_path))
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == [REMOTE, PIN, "src/libch341-spi-userspace"]


def test_an_undeclared_optional_source_is_empty_not_a_failure(tmp_path):
    out = _bash("read_src --optional libch341", _lhpc(tmp_path, declared=False))
    assert out.returncode == 0, out.stderr
    assert out.stdout == ""


def test_a_required_source_still_fails_when_undeclared(tmp_path):
    out = _bash("read_src libch341", _lhpc(tmp_path, declared=False))
    assert out.returncode == 1                      # sys.exit(<message>) in read_src
    assert "component/source not found: libch341" in out.stderr


def test_an_optional_read_fails_when_the_manifest_does_not_load(tmp_path):
    # An unloadable manifest must not read as "this lhpc has no Meshstick library".
    out = _bash('P="$(read_src --optional libch341)"; echo "reached: $P"',
                _lhpc(tmp_path, broken=True))
    assert out.returncode == 1                      # the loader's traceback, through errexit
    assert "reached" not in out.stdout
    assert "manifest does not load" in out.stderr


def _daemon(tmp_path, lib_bytes=None):
    binary = tmp_path / "loraham_daemon" / "loraham_daemon"
    binary.parent.mkdir()
    binary.write_bytes(b"\x7fELF daemon")
    if lib_bytes is not None:
        (binary.parent / "lib").mkdir()
        (binary.parent / "lib" / "libloraham-ch341.so").write_bytes(lib_bytes)
    return binary


def test_the_library_beside_the_binary_with_the_pin_is_accepted(tmp_path):
    binary = _daemon(tmp_path, b"\x7fELF\x00lib\x00" + PIN.encode() + b"\x00")
    out = _bash(f'daemon_lib "{binary}" {PIN}')
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str(binary.parent / "lib" / "libloraham-ch341.so")


def test_a_missing_library_fails_the_build(tmp_path):
    binary = _daemon(tmp_path)
    out = _bash(f'daemon_lib "{binary}" {PIN}')
    assert out.returncode == 5
    assert "Meshstick library not built" in out.stderr


@pytest.mark.parametrize("embedded", ["0" * 40, PIN[:39] + "0"])
def test_a_library_of_another_commit_fails_the_build(tmp_path, embedded):
    binary = _daemon(tmp_path, b"\x7fELF\x00" + embedded.encode() + b"\x00")
    out = _bash(f'daemon_lib "{binary}" {PIN}')
    assert out.returncode == 5
    assert f"does not embed pine64 {PIN}" in out.stderr


@pytest.mark.parametrize("pin", ["", PIN[:12], PIN.upper(), PIN + "0"])
def test_a_pin_that_is_no_full_commit_fails_the_build(tmp_path, pin):
    # An empty pin would match any file (`grep -F ""`): the pin must be a full commit id.
    binary = _daemon(tmp_path, b"\x7fELF\x00" + PIN.encode() + b"\x00")
    out = _bash(f'daemon_lib "{binary}" "{pin}"')
    assert out.returncode == 5
    assert "pine64 pin is not a full commit id" in out.stderr


# ---- the builder's integration points: clone at the pin, deps over both ELFs, staging, dlopen ----

POLICY = LIB.parent / "headless-policy.sh"


def _run(script, env_extra=None, sources=(LIB,)):
    env = {"PATH": os.environ["PATH"], "PY": sys.executable, **(env_extra or {})}
    src = "; ".join(f'source "{s}"' for s in sources)
    return subprocess.run(["bash", "-c", f"set -euo pipefail; {src}; {script}"], env=env,
                          capture_output=True, text=True, timeout=60)


def _git(*args, cwd):
    """Run git in `cwd`; returns HEAD after a commit, "" otherwise."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    if args[0] != "commit":
        return ""
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


def test_a_declared_source_is_checked_out_at_its_pin_not_its_branch_head(tmp_path):
    up = tmp_path / "upstream"
    up.mkdir()
    _git("init", "-q", "-b", "main", cwd=up)
    (up / "f").write_text("1")
    _git("add", "f", cwd=up)
    pin = _git("commit", "-q", "-m", "pinned", cwd=up)
    (up / "f").write_text("2")
    head = _git("commit", "-qam", "newer", cwd=up)
    assert head != pin
    dest = tmp_path / "root" / "src" / "libch341-spi-userspace"
    out = _run(f'clone_pin "{up}" {pin} "{dest}"; git -C "{dest}" rev-parse HEAD')
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == pin


def test_a_pin_the_remote_does_not_have_fails_the_clone(tmp_path):
    up = tmp_path / "upstream"
    up.mkdir()
    _git("init", "-q", "-b", "main", cwd=up)
    (up / "f").write_text("1")
    _git("add", "f", cwd=up)
    head = _git("commit", "-q", "-m", "only", cwd=up)
    dest = tmp_path / "dest"
    out = _run(f'clone_pin "{up}" {"0" * 40} "{dest}"')
    # A real failed checkout: the clone happened and stayed at the remote's head, and git's
    # checkout refused the pin (exit 128, naming it) — not a missing helper (127, no clone).
    assert out.returncode == 128, out.stderr
    assert "0" * 40 in out.stderr
    assert subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip() == head


def _stub_deps(tmp_path, ldd_lines: dict, owners: dict):
    """Fake `ldd` (prints <elf>.ldd) and `dpkg -S` (owners: library path -> package)."""
    bin_ = tmp_path / "stubbin"
    bin_.mkdir()
    (bin_ / "ldd").write_text('#!/bin/sh\ncat "$1.ldd" 2>/dev/null\n')
    table = tmp_path / "owners"
    table.write_text("".join(f"{p} {pkg}\n" for p, pkg in owners.items()))
    (bin_ / "dpkg").write_text(f'#!/bin/sh\n[ "$1" = -S ] || exit 2\n'
                               f'awk -v p="$2" \'$1 == p {{ print $2 ": " p; f = 1 }} '
                               f'END {{ exit !f }}\' "{table}"\n')
    for f in ("ldd", "dpkg"):
        (bin_ / f).chmod(0o755)
    for elf, libs in ldd_lines.items():
        Path(f"{elf}.ldd").write_text("".join(f"\t{Path(x).name} => {x} (0x0)\n" for x in libs))
    return {"PATH": f"{bin_}:{os.environ['PATH']}"}


def _libs(tmp_path, *names):
    d = tmp_path / "usr-lib"
    d.mkdir(exist_ok=True)
    out = []
    for n in names:
        (d / n).write_text("")
        out.append(str(d / n))
    return out


def test_the_library_deps_reach_runtime_deps_beside_the_binary_deps(tmp_path):
    libc, lgpio, usb = _libs(tmp_path, "libc.so.6", "liblgpio.so.1", "libusb-1.0.so.0")
    binary, lib = tmp_path / "loraham_daemon", tmp_path / "libloraham-ch341.so"
    env = _stub_deps(tmp_path, {binary: [libc, lgpio], lib: [libc, usb]},
                     {libc: "libc6", lgpio: "liblgpio1", usb: "libusb-1.0-0"})
    both = _run(f'deps_of_all "{binary}" "{lib}"', env)
    assert both.returncode == 0, both.stderr
    assert both.stdout.split() == ["libc6", "liblgpio1", "libusb-1.0-0"]
    alone = _run(f'deps_of "{binary}"', env)
    assert "libusb-1.0-0" not in alone.stdout.split()      # the binary alone cannot see it


def test_for_the_binary_alone_the_deps_are_exactly_deps_of(tmp_path):
    # The undeclared path: one ELF, so runtime_deps must equal the parent's `deps_of "$BIN"`.
    libc, lgpio = _libs(tmp_path, "libc.so.6", "liblgpio.so.1")
    binary = tmp_path / "loraham_daemon"
    env = _stub_deps(tmp_path, {binary: [lgpio, libc]}, {libc: "libc6", lgpio: "liblgpio1"})
    assert _run(f'deps_of_all "{binary}"', env).stdout == _run(f'deps_of "{binary}"', env).stdout


def _listing(stage: Path):
    return sorted((str(p.relative_to(stage)), oct(p.stat().st_mode & 0o777))
                  for p in stage.rglob("*") if p.is_file())


def test_the_stage_carries_the_library_beside_the_binary_read_only(tmp_path):
    binary, lib = tmp_path / "bin", tmp_path / "lib.so"
    binary.write_bytes(b"\x7fELF")
    binary.chmod(0o755)
    lib.write_bytes(b"\x7fELF")
    lib.chmod(0o755)                       # the build may leave it executable; the stage does not
    stage = tmp_path / "stage"
    out = _run(f'daemon_stage "{stage}" src/loraham-daemon "{binary}" "{lib}"')
    assert out.returncode == 0, out.stderr
    assert _listing(stage) == [
        ("src/loraham-daemon/loraham_daemon/lib/libloraham-ch341.so", "0o644"),
        ("src/loraham-daemon/loraham_daemon/loraham_daemon", "0o755")]


def test_without_a_library_the_stage_is_the_binary_alone(tmp_path):
    binary = tmp_path / "bin"
    binary.write_bytes(b"\x7fELF")
    binary.chmod(0o755)
    stage = tmp_path / "stage"
    out = _run(f'daemon_stage "{stage}" src/loraham-daemon "{binary}" ""')
    assert out.returncode == 0, out.stderr
    assert _listing(stage) == [("src/loraham-daemon/loraham_daemon/loraham_daemon", "0o755")]


def _place(root: Path, data: bytes | None = None, src: Path | None = None):
    so = root / "src" / "loraham-daemon" / "loraham_daemon" / "lib" / "libloraham-ch341.so"
    so.parent.mkdir(parents=True)
    so.write_bytes(src.read_bytes() if src else data)
    return so


def test_a_library_that_cannot_load_fails_the_runtime_test(tmp_path):
    _place(tmp_path / "rt", b"not an ELF")
    out = _run(f'dlopen_check "{tmp_path / "rt"}"', sources=(POLICY,))
    assert out.returncode == 9
    assert "libloraham-ch341.so does not load" in out.stderr


def test_a_loadable_library_passes_the_runtime_test(tmp_path):
    import ctypes.util
    name = ctypes.util.find_library("z")
    real = next((Path(d) / name for d in ("/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu",
                                           "/lib64", "/usr/lib64", "/lib/aarch64-linux-gnu",
                                           "/usr/lib/aarch64-linux-gnu")
                 if name and (Path(d) / name).exists()), None)
    if real is None:
        pytest.skip("no system zlib to stand in for a loadable library")
    _place(tmp_path / "rt", src=real)
    out = _run(f'dlopen_check "{tmp_path / "rt"}"', sources=(POLICY,))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ("dlopen: src/loraham-daemon/loraham_daemon/lib/"
                                  "libloraham-ch341.so loads")


def test_an_artifact_without_the_library_has_nothing_to_load(tmp_path):
    (tmp_path / "rt").mkdir()
    out = _run(f'dlopen_check "{tmp_path / "rt"}"', sources=(POLICY,))
    assert out.returncode == 0 and out.stdout == ""
