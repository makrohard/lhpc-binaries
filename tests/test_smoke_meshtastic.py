"""`smoke.sh meshtastic` checks only what meshtasticd owns: it boots, its web server answers on
:9443 (any HTTP status: the web client is its own artifact, so the root is empty), and its API
port 4403 accepts. Driven with a fake meshtasticd that serves those ports the way the real one
does; `FAKE_MTD` picks what it gets wrong."""
from __future__ import annotations

import os
import shutil
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
        web = http.server.ThreadingHTTPServer(("127.0.0.1", 9443), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(home + "/c.pem", home + "/k.pem")
        web.socket = ctx.wrap_socket(web.socket, server_side=True)
        threading.Thread(target=web.serve_forever, daemon=True).start()
    if mode != "noapi":
        api = socket.socket(); api.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        api.bind(("127.0.0.1", 4403)); api.listen(4)
    time.sleep(300)                        # the smoke kills it; outlive any poll window
    ''')


def _smoke(tmp_path, mode):
    mtd = tmp_path / "root" / "build" / "tools" / "meshtasticd" / "meshtasticd"
    mtd.parent.mkdir(parents=True)                       # no web/ beside it: the C3 layout
    mtd.write_text(FAKE.format(python=sys.executable))
    mtd.chmod(0o755)
    env = dict(os.environ, FAKE_MTD=mode)
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
    assert r.returncode == 7 and "API port 4403 not accepting" in r.stderr, r.stdout + r.stderr


def test_a_dead_daemon_fails(tmp_path):
    r = _smoke(tmp_path, "dead")
    assert r.returncode == 7 and "meshtasticd exited" in r.stderr, r.stdout + r.stderr


def test_a_silent_web_server_fails(tmp_path):
    # Alive, API open, nothing on :9443: the smoke waits out its whole poll window (~80 s).
    r = _smoke(tmp_path, "noweb")
    assert r.returncode == 8 and "web server not answering on :9443" in r.stderr, r.stdout + r.stderr
