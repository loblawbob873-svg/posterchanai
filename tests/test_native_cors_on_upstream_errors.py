"""`blocked by CORS policy` from the desktop app was the server RESTARTING, not a missing allowlist.

The PosterChanOS VM logged, for `https://poster.place/client/stats` from `app://posterchan`:
"No 'Access-Control-Allow-Origin' header is present". The app DOES answer that origin (measured:
`access-control-allow-origin: app://posterchan` on every 200). Both logged failures (07:15:20 and
09:07:29) fell inside server1's two restarts that morning (started 07:15:23; stopped/started 09:07:28,
up 09:07:32) -- the requests reached nginx while the app was down, and nginx's own 502 page carries
no CORS header, so Chromium reports the one thing it can see.

Two fixes, two tests:

* the shipped nginx configs answer an nginx-made 502/503/504 to a NATIVE origin readably and as
  what it is (refused upstream = "restarting" 503 + Retry-After; nginx 503 stays 503; a timeout stays
  504), grant native PREFLIGHTS with a 204, and leave every other origin's error page -- with its
  original status -- and the app's own 503s untouched. Driven against a REAL nginx with a dead, a
  silent and a live upstream;
* `/client/stats` no longer 500s with a NameError when `relay_status()` raises: `relay_confined` was
  bound only inside the try, and an unhandled exception is answered outside the CORS middleware --
  the same symptom from the app's side.
"""
import asyncio
import http.server
import json
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ("nginx/posterchanai.conf.example", "docker/proxy/posterchanai.conf")


def _block(text, name):
    m = re.search(r"# >>> %s\n(.*?)# <<< %s" % (re.escape(name), re.escape(name)), text, re.S)
    assert m, "block %s missing" % name
    return m.group(1)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _req(port, path, origin=None, method="GET", headers=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method)
    if origin:
        req.add_header("Origin", origin)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            status, hdrs, body = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, hdrs, body = e.code, e.headers, e.read()
    return status, {k.lower(): v for k, v in hdrs.items()}, body


@pytest.mark.parametrize("rel", CONFIGS)
def test_the_native_origin_list_matches_the_apps(rel):
    from app.auth import NATIVE_APP_ORIGINS
    text = (ROOT / rel).read_text()
    listed = re.findall(r'"([a-z]+://[^"]+)"\s+\$http_origin;', _block(text, "pc-native-cors-map"))
    assert sorted(listed) == sorted(NATIVE_APP_ORIGINS)
    # Server-wide, one named location PER CODE so each keeps its own status for everyone else.
    block = _block(text, "pc-native-cors-upstream-down")
    for code in (502, 503, 504):
        assert "error_page %d = @pc_upstream_%d;" % (code, code) in block


class _App503(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"the app's own 503")

    def log_message(self, *a):
        pass


def _silent_upstream():
    """Accepts and never answers: an app that is up but too slow (-> nginx 504)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    held = []

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            held.append(c)
    threading.Thread(target=loop, daemon=True).start()
    return srv, held


PREFLIGHT = {"Access-Control-Request-Method": "POST",
             "Access-Control-Request-Headers": "authorization,content-type"}


@pytest.fixture(params=CONFIGS)
def edge(request, tmp_path):
    """A REAL nginx running the shipped block, in front of: a dead upstream (restart), a silent one
    (timeout), an nginx-generated 503, and an app that answers its own 503."""
    if not shutil.which("nginx"):
        pytest.skip("nginx not installed")
    text = (ROOT / request.param).read_text()
    port, dead = _free_port(), _free_port()
    live = http.server.HTTPServer(("127.0.0.1", 0), _App503)
    threading.Thread(target=live.serve_forever, daemon=True).start()
    silent, held = _silent_upstream()
    for d in ("cb", "px", "fc", "uw", "sc"):
        (tmp_path / d).mkdir()
    conf = tmp_path / "nginx.conf"
    conf.write_text("""
worker_processes 1; daemon off; pid %(t)s/nginx.pid; error_log %(t)s/error.log;
events {}
http {
  access_log off;
  client_body_temp_path %(t)s/cb; proxy_temp_path %(t)s/px; fastcgi_temp_path %(t)s/fc;
  uwsgi_temp_path %(t)s/uw; scgi_temp_path %(t)s/sc;
  %(map)s
  server {
    listen 127.0.0.1:%(port)d;
    %(srv)s
    location /client/ { proxy_pass http://127.0.0.1:%(dead)d; }
    location /slow/   { proxy_pass http://127.0.0.1:%(silent)d; proxy_read_timeout 1s; }
    location /busy/   { return 503; }
    location /own503/ { proxy_pass http://127.0.0.1:%(live)d; }
  }
}
""" % {"t": tmp_path, "map": _block(text, "pc-native-cors-map"),
       "srv": _block(text, "pc-native-cors-upstream-down"), "port": port, "dead": dead,
       "silent": silent.getsockname()[1], "live": live.server_address[1]})
    proc = subprocess.Popen(["nginx", "-p", str(tmp_path), "-c", str(conf)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail("nginx refused the config: " + proc.stderr.read().decode())
                time.sleep(0.05)
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        live.shutdown()
        silent.close()
        for c in held:
            c.close()


NATIVE = ("app://posterchan", "https://localhost")
OTHERS = (None, "https://evil.example")


def _granted(h, origin):
    return h.get("access-control-allow-origin") == origin and h.get("access-control-allow-credentials") == "true"


def test_a_restart_is_readable_by_the_native_apps_and_unchanged_for_everyone_else(edge):
    for origin in NATIVE:
        status, h, body = _req(edge, "/client/stats?v=x", origin)
        assert status == 503, (origin, status, body)
        assert _granted(h, origin) and h.get("retry-after") == "5"
        assert json.loads(body) == {"error": "the server is restarting"}
    for origin in OTHERS:
        status, h, _ = _req(edge, "/client/stats", origin)
        assert status == 502, (origin, status)
        assert "access-control-allow-origin" not in h


def test_a_preflight_during_a_restart_is_granted_to_the_native_apps(edge):
    """Anything non-simple -- an Authorization header, a JSON POST, PUT/DELETE -- is preflighted,
    and a preflight answered 503 fails CORS before the real request is ever sent."""
    for path in ("/client/stats", "/slow/x", "/busy/x"):
        for origin in NATIVE:
            status, h, _ = _req(edge, path, origin, "OPTIONS", PREFLIGHT)
            assert status == 204, (path, origin, status)
            assert _granted(h, origin)
            assert "POST" in h.get("access-control-allow-methods", "")
            assert h.get("access-control-allow-headers") == "authorization,content-type"
        for origin in OTHERS:
            status, h, _ = _req(edge, path, origin, "OPTIONS", PREFLIGHT)
            assert status in (502, 503, 504) and "access-control-allow-origin" not in h, (path, origin, status)


def test_a_timeout_stays_a_504_and_is_not_called_a_restart(edge):
    for origin in NATIVE:
        status, h, body = _req(edge, "/slow/x", origin)
        assert status == 504, (origin, status)
        assert _granted(h, origin)
        assert "restarting" not in body.decode()
    for origin in OTHERS:
        status, h, _ = _req(edge, "/slow/x", origin)
        assert status == 504, (origin, status)
        assert "access-control-allow-origin" not in h


def test_an_nginx_503_keeps_its_status(edge):
    for origin in NATIVE:
        status, h, body = _req(edge, "/busy/x", origin)
        assert status == 503 and _granted(h, origin)
        assert "restarting" not in body.decode()
    for origin in OTHERS:
        status, h, _ = _req(edge, "/busy/x", origin)
        assert status == 503, (origin, status)
        assert "access-control-allow-origin" not in h


def test_the_apps_own_503_passes_through_untouched(edge):
    status, h, body = _req(edge, "/own503/x", "app://posterchan")
    assert status == 503 and body == b"the app's own 503"
    assert "access-control-allow-origin" not in h


def test_client_stats_answers_when_the_relay_status_cannot_be_read(monkeypatch):
    from app.routers import client as cl
    from app.services.nostr_relay import thread as th

    def boom():
        raise RuntimeError("relay subprocess not up yet")

    async def zero():
        return 0

    monkeypatch.setattr(th, "relay_status", boom)
    monkeypatch.setattr(cl, "_live_stream_count", zero)

    class _Req:
        headers = {"x-forwarded-for": "41.90.1.9"}
        client = type("c", (), {"host": "41.90.1.9"})()

    resp = asyncio.run(cl.client_stats(_Req(), v="k" + "b" * 16))
    body = json.loads(bytes(resp.body))
    assert resp.status_code == 200
    assert body["relay_confined"] == 0 and body["relay"] == 0
