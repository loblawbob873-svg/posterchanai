"""Concord calls on a ROLE-SPLIT node (2026-10-22, after deploy #20: the broker answered 404 on poster.place and no
livekit-server ran). Two separate faults, both invisible to a one-process test:

1. role_runner's media role listed streams and TURN but not LiveKit, so the media process never started it.
2. The broker (web process) asked `_proc`, which only the spawning process has -- so even a running SFU read as absent.
"""
import http.server
import threading

from app import role_runner
from app.services import livekit_service as L


def test_the_media_role_supervises_livekit_beside_turn():
    media = {(m, start, stop) for _, m, start, stop in role_runner._ROLE_SERVICES["media"]}
    assert ("app.services.livekit_service", "start_livekit_server", "stop_livekit_server") in media
    assert ("app.services.turn_service", "start_turn_server", "stop_turn_server") in media


def test_main_and_the_media_role_start_the_same_media_components():
    """The single-process path (main.py) and the split path (role_runner) must not drift again."""
    from pathlib import Path
    main = (Path(__file__).resolve().parents[1] / "app/main.py").read_text()
    for _, module, start, _ in role_runner._ROLE_SERVICES["media"]:
        assert f"if _owns('media'): {start}()" in main, start


def _serve(body, status=200):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _ask(monkeypatch, port):
    monkeypatch.setattr(L, "_proc", None)
    monkeypatch.setattr(L, "_cfg", lambda: {"livekit_http_port": str(port)})
    L._seen.update(at=0.0, up=False)
    return L.available()


def test_another_process_s_livekit_counts_as_available(monkeypatch):
    srv = _serve(b"OK")
    try:
        assert _ask(monkeypatch, srv.server_address[1]) is True
    finally:
        srv.shutdown()


def test_nothing_listening_or_something_else_is_not_livekit(monkeypatch):
    srv = _serve(b"<html>hello</html>")
    try:
        assert _ask(monkeypatch, srv.server_address[1]) is False
    finally:
        srv.shutdown()
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); free = s.getsockname()[1]; s.close()
    assert _ask(monkeypatch, free) is False


def test_the_answer_is_cached_so_the_probe_costs_one_request_per_5_s(monkeypatch):
    srv = _serve(b"OK")
    port = srv.server_address[1]
    try:
        assert _ask(monkeypatch, port) is True
    finally:
        srv.shutdown(); srv.server_close()
    assert L.available() is True, "a second ask inside 5 s must not hit the port"
