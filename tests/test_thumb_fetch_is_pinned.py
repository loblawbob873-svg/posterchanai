"""/client/thumb: the fetch connects to the address the guard judged, and junk URLs cannot grow the cache unbounded.

Security review 2026-10-18: (1) `is_safe_url` and the redirect hook each resolved the name, then httpx resolved it
AGAIN to connect -- a name answering public-then-loopback (DNS rebinding, TTL 0) passed both checks and was fetched
from this node's own network, the result resized and served back to anyone; (2) every refused or failed fetch left
an EMPTY "use the original" marker, and the cache cap counted bytes, so markers were never pruned.
"""
import asyncio
import io
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.services import search_service, thumb_service


def _png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (800, 600), (200, 30, 30)).save(b, "PNG")
    return b.getvalue()


class _Internal(BaseHTTPRequestHandler):
    body = _png()
    hits = 0

    def do_GET(self):
        _Internal.hits += 1
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


def test_a_rebinding_name_cannot_reach_this_nodes_network(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Internal)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real = socket.getaddrinfo
    asked = {"n": 0}

    def rebinding(host, *a, **k):                    # public for the checks, loopback for the connect
        if (host.decode() if isinstance(host, bytes) else host) == "rebind.example":   # anyio passes IDNA bytes
            asked["n"] += 1
            ip = "93.184.216.34" if asked["n"] <= 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        return real(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "93.184.216.34" if h == "rebind.example" else h)
    monkeypatch.setattr(search_service, "own_media_hosts", lambda: set())
    try:
        got = asyncio.run(thumb_service._fetch("http://rebind.example:%d/secret.png" % port))
    except Exception:                                 # refused is the right answer, as an error or as None
        got = None
    finally:
        srv.shutdown()
    assert got is None and _Internal.hits == 0, "the fetch reached 127.0.0.1 (%d request(s))" % _Internal.hits


def test_empty_markers_are_pruned_like_any_file(tmp_path, monkeypatch):
    monkeypatch.setattr(thumb_service, "_ROOT", str(tmp_path))
    monkeypatch.setattr(thumb_service, "CACHE_CAP", 100 * 4096)
    for i in range(1000):                             # 1,000 junk URLs, each remembered as "use the original"
        p = thumb_service._path("http://127.0.0.1/%d" % i, 320)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").close()
    thumb_service._prune()
    left = sum(len(f) for _d, _s, f in os.walk(str(tmp_path)))
    assert left <= 100, "%d empty markers survived a prune with room for 100 files" % left
