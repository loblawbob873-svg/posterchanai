"""`blocked by CORS policy` from the desktop app was the server RESTARTING, not a missing allowlist.

The PosterChanOS VM logged, for `https://poster.place/client/stats` from `app://posterchan`:
"No 'Access-Control-Allow-Origin' header is present". The app DOES answer that origin (measured:
`access-control-allow-origin: app://posterchan` on every 200). Both logged failures (07:15:20 and
09:07:29) fell inside server1's two restarts that morning (started 07:15:23; stopped/started 09:07:28,
up 09:07:32) -- the requests reached nginx while the app was down, and nginx's own 502 page carries
no CORS header, so Chromium reports the one thing it can see.

Two fixes, two tests:

* the shipped nginx configs answer an upstream failure to a NATIVE origin with a readable 503 +
  Retry-After (and leave every other request's error page, and the app's own 503s, untouched) --
  driven against a REAL nginx with a dead upstream and a live one;
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


def _get(port, path, origin=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.mark.parametrize("rel", CONFIGS)
def test_the_native_origin_list_matches_the_apps(rel):
    from app.auth import NATIVE_APP_ORIGINS
    text = (ROOT / rel).read_text()
    listed = re.findall(r'"([a-z]+://[^"]+)"\s+\$http_origin;', _block(text, "pc-native-cors-map"))
    assert sorted(listed) == sorted(NATIVE_APP_ORIGINS)
    # Server-wide, so it covers /client/*, /api/*, /ws/* and the rest alike.
    assert "error_page 502 503 504 = @pc_upstream_down;" in _block(text, "pc-native-cors-upstream-down")


class _App503(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"the app's own 503")

    def log_message(self, *a):
        pass


@pytest.mark.skipif(not shutil.which("nginx"), reason="nginx not installed")
@pytest.mark.parametrize("rel", CONFIGS)
def test_a_restarting_server_is_readable_by_the_native_apps(rel, tmp_path):
    text = (ROOT / rel).read_text()
    port, dead = _free_port(), _free_port()   # nothing listens on `dead`: the app mid-restart
    live = http.server.HTTPServer(("127.0.0.1", 0), _App503)
    threading.Thread(target=live.serve_forever, daemon=True).start()
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
    location /own503/ { proxy_pass http://127.0.0.1:%(live)d; }
  }
}
""" % {"t": tmp_path, "map": _block(text, "pc-native-cors-map"),
       "srv": _block(text, "pc-native-cors-upstream-down"), "port": port, "dead": dead,
       "live": live.server_address[1]})
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

        # The desktop app, while the server restarts: a response it may READ, saying so.
        status, headers, body = _get(port, "/client/stats?v=x", "app://posterchan")
        h = {k.lower(): v for k, v in headers.items()}
        assert status == 503, (status, body)
        assert h.get("access-control-allow-origin") == "app://posterchan"
        assert h.get("access-control-allow-credentials") == "true"
        assert h.get("retry-after") == "5"
        assert json.loads(body) == {"error": "the server is restarting"}

        # The APK's origin too.
        _, headers, _ = _get(port, "/client/stats", "https://localhost")
        assert {k.lower(): v for k, v in headers.items()}.get("access-control-allow-origin") == "https://localhost"

        # Anyone else -- a browser tab, a stranger's origin -- keeps nginx's ordinary 502, and is
        # never handed a credentialed CORS grant.
        for origin in (None, "https://evil.example"):
            status, headers, _ = _get(port, "/client/stats", origin)
            h = {k.lower(): v for k, v in headers.items()}
            assert status == 502, (origin, status)
            assert "access-control-allow-origin" not in h

        # The app's OWN 503 is the app's answer and passes through untouched.
        status, headers, body = _get(port, "/own503/x", "app://posterchan")
        assert status == 503 and body == b"the app's own 503"
        assert "access-control-allow-origin" not in {k.lower() for k in headers}
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        live.shutdown()


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
