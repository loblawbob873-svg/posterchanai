"""A spend must not be the request that rebuilds monerod's output distribution.

The fake daemon below is a port of the cache in monerod's rpc::RpcHandler::get_output_distribution
(src/rpc/rpc_handler.cpp, v0.18): ONE in-memory slot keyed on (amount 0, from_height, to_height), replaced
by every amount-0 answer, `to_height 0` read as the tip, EMPTY after a restart. A wallet's transfer asks for
the full chain (measured through a logging proxy on nas.lan: two .bin requests, both that key). Measured:
0.04 s cached, 14 s warm rebuild, ~5 min cold — the first zap after a monerod restart timed out and built
nothing (2026-09-25).
"""
import importlib.util, json, pathlib, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("warm", ROOT / "scripts" / "monero_distribution_warm.py")
warm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(warm)

TIP = 3770613
FORK = 1546000
#: What wallet2 asks while building a transaction (get_rct_distribution).
WALLET_FULL = {"amounts": [0], "from_height": 0, "to_height": 0, "binary": True, "compress": True}
#: Any OTHER amount-0 range — an outside wallet on the public node — replaces the slot.
FOREIGN_RANGE = {"amounts": [0], "from_height": FORK - 4765, "to_height": FORK + 1,
                 "cumulative": True, "binary": True}


class FakeMonerod:
    """Single-slot distribution cache, as monerod has it. Counts the full rebuilds it had to do."""

    def __init__(self):
        self.slot = None           # (from_height, to_height) currently cached
        self.rebuilds = []         # key of every request that missed

    def distribution(self, p):
        if p.get("amounts") != [0]:
            raise AssertionError("only amount-0 is modelled")
        key = (p.get("from_height", 0), p.get("to_height") or TIP - 1)
        if self.slot != key:
            self.rebuilds.append(key)
        self.slot = key            # every amount-0 answer overwrites the slot
        return {"status": "OK", "distributions": [{"amount": 0}]}

    def restart(self):
        self.slot = None

    def spend(self):
        """The distribution request one wallet transaction makes."""
        self.distribution(WALLET_FULL)

    def serve(self):
        daemon = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert body["method"] == "get_output_distribution"
                res = daemon.distribution(body["params"])
                # monerod's binary+compress reply: raw bytes inside a JSON string, NOT valid UTF-8.
                out = (b'{"jsonrpc": "2.0", "id": 0, "result": {"distributions": [{"amount": 0, '
                       b'"compress_binary": true, "distribution": "\xe3\x9f\x00\xff", "start_height": 0}], '
                       b'"status": "' + res["status"].encode() + b'", "untrusted": false}}')
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}/json_rpc"


FULL_KEY = (0, TIP - 1)


def test_without_the_warmer_the_first_spend_after_a_restart_rebuilds_from_block_zero():
    """The failure, reproduced: what a zap paid on nas.lan. If this stops failing the model is wrong."""
    d = FakeMonerod()
    d.spend()
    d.restart()
    d.rebuilds.clear()
    d.spend()
    assert FULL_KEY in d.rebuilds


def test_a_normal_spend_does_not_evict_the_cache():
    """Measured through a logging proxy: a transfer asks only for the full chain, so spends keep hitting."""
    d = FakeMonerod()
    d.spend()
    d.rebuilds.clear()
    for _ in range(3):
        d.spend()
    assert d.rebuilds == []


def test_with_the_warmer_no_spend_is_ever_the_first_request():
    d = FakeMonerod()
    srv, url = d.serve()
    try:
        for disturb in (d.restart, lambda: d.distribution(FOREIGN_RANGE), d.restart):
            disturb()                      # a restart, or an outside wallet's different range
            ok, _, detail = warm.warm_once(url, timeout=5)
            assert ok, detail
            d.rebuilds.clear()
            d.spend()
            assert FULL_KEY not in d.rebuilds, "the spend paid the rebuild the warmer exists to pay"
    finally:
        srv.shutdown()


def test_the_warmer_asks_for_exactly_the_wallets_cache_key():
    """A different from/to primes a different entry AND evicts the wallet's — worse than doing nothing."""
    p = warm.request_params()
    assert p["amounts"] == [0]
    assert p["from_height"] == WALLET_FULL["from_height"]
    assert p["to_height"] == WALLET_FULL["to_height"]


def test_a_daemon_that_cannot_be_asked_is_reported_not_raised():
    ok, _, detail = warm.warm_once("http://127.0.0.1:9/json_rpc", timeout=2)
    assert not ok and detail
