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
        # Like meshtasticd, the web port is the one its config (-c) names; unlike it, the fake binds
        # nothing: it serves on the listening socket the test holds and hands it (FAKE_WEB_FD).
        sock = socket.socket(fileno=int(os.environ["FAKE_WEB_FD"]))
        conf = open(sys.argv[sys.argv.index("-c") + 1]).read()
        port = int(conf.split("Webserver:")[1].split("Port:")[1].split()[0])
        if port != sock.getsockname()[1]:
            print("FAKE: config web port", port, "is not the given socket's", sock.getsockname()[1],
                  file=sys.stderr)
            sys.exit(9)
        web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H, bind_and_activate=False)
        web.socket.close()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(home + "/c.pem", home + "/k.pem")
        web.socket = ctx.wrap_socket(sock, server_side=True)
        threading.Thread(target=web.serve_forever, daemon=True).start()
    if mode != "noapi":
        api = socket.socket(fileno=int(os.environ["FAKE_API_FD"]))   # already listening
    time.sleep(300)                        # the smoke kills it; outlive any poll window
    ''')


def _reserved():
    """Ports a fake must never sit on: meshtasticd's 9443/4403 and, when the controller's source is
    at hand (LHPC_SRC), every port its manifest names."""
    ports = {9443, 4403}
    src = os.environ.get("LHPC_SRC", "")
    manifest = Path(src, "lhpc", "data", "manifest.example.toml")
    if src and manifest.is_file():
        ports |= {int(p) for p in re.findall(r"\{port:(\d+)\}", manifest.read_text())}
    return ports


def _hold_ports(listening, draws=20):
    """One socket per entry, on a port the OS hands out (bind to 0) that is not reserved (a
    reserved one is drawn again). Listening where asked, else only bound, so its port is closed (a
    connect is refused) and still nobody else's. The test keeps them open for the whole smoke; the
    fake serves on the listening ones it is handed and binds nothing, so no port is ever released
    before use."""
    reserved, held = _reserved(), []
    try:
        for listen in listening:
            for _ in range(draws):
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                if sock.getsockname()[1] not in reserved:
                    break
                sock.close()
            else:
                raise RuntimeError("the OS kept handing out reserved ports")
            if listen:
                sock.listen(8)
            sock.set_inheritable(True)
            held.append(sock)
        return held
    except BaseException:
        for sock in held:
            sock.close()
        raise


def _smoke(tmp_path, mode):
    mtd = tmp_path / "root" / "build" / "tools" / "meshtasticd" / "meshtasticd"
    mtd.parent.mkdir(parents=True)                       # no web/ beside it: the C3 layout
    mtd.write_text(FAKE.format(python=sys.executable))
    mtd.chmod(0o755)
    web, api = _hold_ports([mode != "noweb", mode != "noapi"])
    try:
        ports = [web.getsockname()[1], api.getsockname()[1]]
        assert not set(ports) & _reserved(), ports
        env = dict(os.environ, FAKE_MTD=mode, SMOKE_WEB_PORT=str(ports[0]),
                   SMOKE_API_PORT=str(ports[1]), FAKE_WEB_FD=str(web.fileno()),
                   FAKE_API_FD=str(api.fileno()))
        r = subprocess.run(["bash", str(SMOKE), "meshtastic", str(tmp_path / "root")],
                           capture_output=True, text=True, timeout=120, env=env,
                           pass_fds=(web.fileno(), api.fileno()))
        r.ports = ports
        return r
    finally:
        web.close()
        api.close()


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


def test_a_reserved_port_the_os_hands_out_is_drawn_again(monkeypatch):
    """Should the OS hand out 4403 or 9443 (a host whose free-port range holds them), the helper
    draws again; the ports it holds are never reserved."""
    hand_out = iter([4403, 9443, 51001, 51002])

    class Sock:
        def __init__(self):
            self.port = None

        def bind(self, addr):
            self.port = next(hand_out)

        def getsockname(self):
            return ("127.0.0.1", self.port)

        def listen(self, n):
            pass

        def set_inheritable(self, flag):
            pass

        def close(self):
            pass
    monkeypatch.setattr(socket, "socket", Sock)
    assert [s.getsockname()[1] for s in _hold_ports([True, True])] == [51001, 51002]


def test_the_smoke_polls_the_ports_the_fake_was_handed_and_none_is_reserved(tmp_path):
    r = _smoke(tmp_path, "ok")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"polling https://127.0.0.1:{r.ports[0]}/ and TCP {r.ports[1]}" in r.stdout
    assert not set(r.ports) & _reserved()
    assert not re.search(r"\b(?:9443|4403)\b", FAKE), "the fake binds only the ports it is given"
    src = os.environ.get("LHPC_SRC", "")
    if src and Path(src, "lhpc", "data", "manifest.example.toml").is_file():
        assert {4403, 9443} < _reserved(), "the manifest's port placeholders were not found"


def test_without_the_test_ports_the_smoke_polls_meshtasticds_own():
    """The real build smokes the real daemon on its own ports: the overrides only move a fake."""
    text = SMOKE.read_text()
    assert 'web_port="${SMOKE_WEB_PORT:-9443}"' in text and 'api_port="${SMOKE_API_PORT:-4403}"' in text


def test_the_fake_refuses_a_config_that_names_another_web_port(tmp_path):
    """The fake serves only where it was handed a socket; a config naming another web port (a smoke
    that wrote the wrong one) stops it, as a real meshtasticd would serve where nobody polls."""
    fake = tmp_path / "meshtasticd"
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(0o755)
    conf = tmp_path / "config.yaml"
    (web,) = _hold_ports([True])
    try:
        conf.write_text(f"Webserver:\n  Port: {web.getsockname()[1] + 1}\n")
        r = subprocess.run([str(fake), "-c", str(conf)], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, HOME=str(tmp_path), FAKE_WEB_FD=str(web.fileno())),
                           pass_fds=(web.fileno(),))
    finally:
        web.close()
    assert r.returncode == 9 and "is not the given socket's" in r.stderr, r.stdout + r.stderr
