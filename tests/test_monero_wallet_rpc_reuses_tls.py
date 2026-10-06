"""Per-user wallet RPCs build ONE TLS context, not one per call.

py-spy on server1's worker (2026-10-06): ssl.create_default_context was 48% of the worker's CPU, almost
all of it from UserWallets.rpc(), which makes an httpx client per call -- and httpx loads the whole system
CA store into a fresh context for each. Drives the real rpc() against a local JSON-RPC stub and counts the
contexts built. (Approved explicitly for the wallet code: "finish the remaining tasks, 1 2 3".)
"""
import asyncio
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.services import monero_user_wallets as muw


class _RPC(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        out = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": {"height": 7}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_twenty_wallet_calls_build_at_most_one_tls_context(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _RPC)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        built = []
        real = ssl.create_default_context
        monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: built.append(1) or real(*a, **k))
        monkeypatch.setattr(muw, "_TLS", None)
        w = muw.UserWallets.__new__(muw.UserWallets)
        w.url, w.user, w.password, w.timeout = f"http://127.0.0.1:{srv.server_port}/json_rpc", "u", "p", 8.0

        async def calls():
            return [await w.rpc("get_height") for _ in range(20)]
        results = asyncio.run(calls())
        assert all(r == {"height": 7} for r in results), results[:2]
        assert len(built) <= 1, f"{len(built)} TLS contexts built for 20 wallet calls"
    finally:
        srv.shutdown()
