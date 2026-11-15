"""`smoke.sh meshtastic` checks only what meshtasticd owns: it boots, its web server answers on
:9443 (any HTTP status: the web client is its own artifact, so the root is empty), and its API
port accepts. Driven with a fake meshtasticd that serves those ports the way the real one
does; `FAKE_MTD` picks what it gets wrong."""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SMOKE = Path(__file__).resolve().parents[1] / "builders" / "smoke.sh"

FAKE = textwrap.dedent('''\
    #!{python}
    import http.server, os, socket, ssl, subprocess, sys, threading, time
    if sys.argv[1:] == ["--version"]:
        print("meshtasticd 0.0.0-fake"); sys.exit(0)
    mode = os.environ.get("FAKE_MTD", "ok")
    if mode == "dead":
        sys.exit(3)
    home = os.environ["HOME"]
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-keyout", home + "/k.pem", "-out", home + "/c.pem"],
                   check=True, capture_output=True)
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404); self.end_headers()
        def log_message(self, *a):
            pass
    if mode != "noweb":
        # Like meshtasticd: the web port is the one its config (-c) names.
        conf = open(sys.argv[sys.argv.index("-c") + 1]).read()
        port = int(conf.split("Webserver:")[1].split("Port:")[1].split()[0])
        web = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(home + "/c.pem", home + "/k.pem")
        web.socket = ctx.wrap_socket(web.socket, server_side=True)
        threading.Thread(target=web.serve_forever, daemon=True).start()
    if mode != "noapi":
        api = socket.socket(); api.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        api.bind(("127.0.0.1", int(os.environ["SMOKE_API_PORT"]))); api.listen(4)
    time.sleep(300)                        # the smoke kills it; outlive any poll window
    ''')


def _free_ports(n=2):
    """Ports the OS hands out as free (bind to 0): never meshtasticd's real 9443/4403, so a fake
    here can never be mistaken for a lab's running stack."""
    socks = [socket.socket() for _ in range(n)]
    try:
        for s in socks:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def _smoke(tmp_path, mode):
    mtd = tmp_path / "root" / "build" / "tools" / "meshtasticd" / "meshtasticd"
    mtd.parent.mkdir(parents=True)                       # no web/ beside it: the C3 layout
    mtd.write_text(FAKE.format(python=sys.executable))
    mtd.chmod(0o755)
    web, api = _free_ports()
    env = dict(os.environ, FAKE_MTD=mode, SMOKE_WEB_PORT=str(web), SMOKE_API_PORT=str(api))
    return subprocess.run(["bash", str(SMOKE), "meshtastic", str(tmp_path / "root")],
                          capture_output=True, text=True, timeout=120, env=env)


pytestmark = pytest.mark.skipif(not (shutil.which("curl") and shutil.which("openssl")),
                                reason="needs curl and openssl")


def test_a_daemon_without_a_web_client_passes(tmp_path):
    r = _smoke(tmp_path, "ok")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "web server HTTP 404" in r.stdout and "SMOKE: PASS" in r.stdout


def test_a_closed_api_port_fails(tmp_path):
    r = _smoke(tmp_path, "noapi")
    assert r.returncode == 7 and re.search(r"API port \d+ not accepting", r.stderr), r.stdout + r.stderr


def test_a_dead_daemon_fails(tmp_path):
    r = _smoke(tmp_path, "dead")
    assert r.returncode == 7 and "meshtasticd exited" in r.stderr, r.stdout + r.stderr


def test_a_silent_web_server_fails(tmp_path):
    # Alive, API open, nothing on the web port: the smoke waits out its whole poll window (~80 s).
    r = _smoke(tmp_path, "noweb")
    assert r.returncode == 8 and re.search(r"web server not answering on :\d+", r.stderr), r.stdout + r.stderr


def test_the_fakes_ports_are_no_real_stacks_ports():
    """The fake binds what `_free_ports` returns: never meshtasticd's 9443/4403, and, when the
    controller's source is at hand (LHPC_SRC, as in CI's contract step), no port its manifest names."""
    ports = set(_free_ports())
    assert not ports & {9443, 4403}
    assert not re.search(r"\b(?:9443|4403)\b", FAKE), "the fake binds only the ports it is given"
    src = os.environ.get("LHPC_SRC", "")
    manifest = Path(src, "lhpc", "data", "manifest.example.toml")
    if not src or not manifest.is_file():
        pytest.skip("no controller manifest (LHPC_SRC) to compare with")
    named = {int(p) for p in re.findall(r"\{port:(\d+)\}", manifest.read_text())}
    assert {4403, 9443} <= named, "the manifest's port placeholders were not found"
    assert not ports & named


def test_without_the_test_ports_the_smoke_polls_meshtasticds_own():
    """The real build smokes the real daemon on its own ports: the overrides only move a fake."""
    text = SMOKE.read_text()
    assert 'web_port="${SMOKE_WEB_PORT:-9443}"' in text and 'api_port="${SMOKE_API_PORT:-4403}"' in text
