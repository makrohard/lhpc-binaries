"""`smoke.sh meshcom-bridge` (the bridge part of `smoke.sh meshcom`) starts the packaged MeshCom bridge on its
fake backend and checks that it serves TCP. A bridge from 0.3.0 on refuses to start without its TX power ceiling
(`--max-power`, required); an older one refuses that flag as unknown. The smoke passes `--max-power 10` exactly
when the bridge's own --help names it. Driven with a fake bridge that behaves like each version."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SMOKE = Path(__file__).resolve().parents[1] / "builders" / "smoke.sh"

FAKE = textwrap.dedent('''\
    #!{python}
    import os, socket, sys, time
    args = sys.argv[1:]
    new = os.environ["FAKE_BRIDGE"] == "0.3.0"
    if args == ["--version"]:
        print("meshcom-loraham-bridge " + os.environ["FAKE_BRIDGE"]); sys.exit(0)
    if args == ["--help"]:
        print("usage: meshcom-loraham-bridge --backend fake|loraham [--bind ADDR] [--port N]"
              + (" --max-power N" if new else ""), file=sys.stderr)
        sys.exit(0)
    with open(os.environ["FAKE_ARGS"], "a") as f:
        f.write("\\x00".join(args) + "\\n")
    if new and "--max-power" not in args:
        print("error: --max-power is required (the TX power ceiling, dBm)", file=sys.stderr); sys.exit(2)
    if not new and "--max-power" in args:
        print("error: unknown option --max-power", file=sys.stderr); sys.exit(2)
    # Like the bridge, it serves on the port --port names; unlike it, it binds nothing: it serves on the
    # listening socket the test holds and hands it (FAKE_FD), so no port is ever released before use.
    sock = socket.socket(fileno=int(os.environ["FAKE_FD"]))
    if int(args[args.index("--port") + 1]) != sock.getsockname()[1]:
        print("FAKE: --port is not the given socket's", file=sys.stderr); sys.exit(9)
    time.sleep(300)                        # the smoke kills it; outlive its 2 s startup look
    ''')


def _smoke(tmp_path, version):
    root = tmp_path / "root"
    fake = root / "src" / "meshcom-loraham-bridge" / "build" / "meshcom-loraham-bridge"
    fake.parent.mkdir(parents=True)
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(0o755)
    record = tmp_path / "args"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        env = {**os.environ, "FAKE_BRIDGE": version, "FAKE_ARGS": str(record), "FAKE_FD": str(sock.fileno()),
               "SMOKE_BRIDGE_PORT": str(sock.getsockname()[1]), "TMPDIR": str(tmp_path)}
        r = subprocess.run(["bash", str(SMOKE), "meshcom-bridge", str(root)], env=env, capture_output=True,
                           text=True, timeout=60, pass_fds=(sock.fileno(),), stdin=subprocess.DEVNULL)
    starts = [ln.split("\x00") for ln in record.read_text().splitlines()] if record.exists() else []
    return r, starts


@pytest.mark.parametrize("version, ceiling", [("0.3.0", ["--max-power", "10"]), ("0.2.0", [])],
                         ids=["requires-the-ceiling", "before-the-ceiling"])
def test_the_bridge_starts_with_the_ceiling_exactly_when_its_help_names_it(tmp_path, version, ceiling):
    r, starts = _smoke(tmp_path, version)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SMOKE: PASS (bridge starts with fake backend" in r.stdout, r.stdout
    assert len(starts) == 1, starts                                    # one start, no retry without the flag
    argv = starts[0]
    assert argv[:2] == ["--backend", "fake"], argv                     # the fake backend: no radio, no RF
    if ceiling:
        assert argv[argv.index("--max-power"):] == ceiling, argv       # the ceiling at the end, 10 dBm
    else:
        assert "--max-power" not in argv, argv                         # an older bridge would refuse it
