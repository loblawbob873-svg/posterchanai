"""scripts/check_live_production.py decides PASS / FAIL / SKIP from what production answers. These feed the shipped
check canned answers (no network) and pin the rule that matters most: "could not ask" is NEVER a pass."""
import importlib.util
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("clp", os.path.join(REPO, "scripts", "check_live_production.py"))
clp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clp)


class _R:
    def __init__(self, status, body=None, text="", ctype="application/json"):
        self.status_code, self._body, self.text = status, body, text
        self.headers = {"content-type": ctype}
        self.content = text.encode()

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _serve(monkeypatch, table):
    def fake(url, timeout=None):
        for suffix, resp in table.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(url)
    monkeypatch.setattr(clp, "_http", fake)


GOOD_STATS = {"relay_unavailable": False, "relay": {"running": True, "online": 3},
              "totals": {"events": 10, "notes": 4}, "relay_backend": "posterchandb"}


def test_stats_pass_only_with_real_relay_figures(monkeypatch):
    _serve(monkeypatch, {"/client/server-stats": _R(200, GOOD_STATS)})
    assert clp.check_server_stats("https://x")[0] == clp.PASS
    for broken in ({**GOOD_STATS, "relay_unavailable": True},
                   {**GOOD_STATS, "totals": {"events": None, "notes": 4}},
                   {**GOOD_STATS, "relay": {"running": False}}):
        _serve(monkeypatch, {"/client/server-stats": _R(200, broken)})
        assert clp.check_server_stats("https://x")[0] == clp.FAIL, broken


def test_unreachable_and_503_are_skips_never_passes(monkeypatch):
    _serve(monkeypatch, {"/client/server-stats": _R(503, {"error": "unavailable"}),
                         "/client/federation": _R(503, {"state": "unknown"})})
    assert clp.check_server_stats("https://x")[0] == clp.SKIP
    assert clp.check_federation("https://x")[0] == clp.SKIP
    _serve(monkeypatch, {"/client/server-stats": ConnectionError(), "/": ConnectionError()})
    assert clp.check_server_stats("https://x")[0] == clp.SKIP
    assert clp.check_client_shell("https://x")[0] == clp.SKIP


@pytest.mark.parametrize("state,verdict", [("ok", "PASS"), ("off", "PASS"), ("behind", "FAIL"),
                                           ("failing", "FAIL"), ("unknown", "SKIP")])
def test_federation_state(monkeypatch, state, verdict):
    _serve(monkeypatch, {"/client/federation": _R(200, {"state": state, "lag": 0})})
    assert clp.check_federation("https://x")[0] == verdict


def test_client_shell_reports_the_sw_version(monkeypatch):
    html = _R(200, text="<html><script></script>", ctype="text/html")
    _serve(monkeypatch, {"/client/sw.js": _R(200, text="const CACHE = 'pc-nostr-v2149';\n"), "https://x/": html})
    v, why = clp.check_client_shell("https://x")
    assert v == clp.PASS and "pc-nostr-v2149" in why
    _serve(monkeypatch, {"/client/sw.js": _R(200, text="// nothing"), "https://x/": html})
    assert clp.check_client_shell("https://x")[0] == clp.FAIL


def _main_with(monkeypatch, verdicts):
    monkeypatch.setattr(clp, "wait_ready", lambda base: (True, {"relay_url": "wss://r"}))
    names = ["check_server_stats", "check_relay_publish", "check_concord_ticket", "check_client_shell",
             "check_federation"]
    for n, v in zip(names, verdicts):
        monkeypatch.setattr(clp, n, lambda *_a, _v=v: (_v, "stub"))
    return clp.main(["x", "https://x"])


def test_exit_codes(monkeypatch):
    P, F, S = clp.PASS, clp.FAIL, clp.SKIP
    assert _main_with(monkeypatch, [P] * 5) == 0
    assert _main_with(monkeypatch, [P, P, S, P, P]) == 2, "a could-not-ask must never read as a pass"
    assert _main_with(monkeypatch, [P, F, S, P, P]) == 1
    monkeypatch.setattr(clp, "wait_ready", lambda base: (False, "HTTP 502"))
    assert clp.main(["x", "https://x"]) == 2
    assert clp.main(["x"]) == 2
