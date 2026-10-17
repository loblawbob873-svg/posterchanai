"""The sidebar's "online" counts the app's own viewer ids, not whatever a request names.

2026-10-17: probing /client/stats with ?v=probe-not-a-person made the figure read 8 instead of 7 for the next
2.5 minutes -- any made-up id was a person, so a loop of curl requests could print any number on every sidebar.
"""
from types import SimpleNamespace

from app.routers import client as c


def _req(ip="41.0.0.1"):
    return SimpleNamespace(headers={}, client=SimpleNamespace(host=ip))


def test_only_the_clients_own_viewer_ids_are_people(monkeypatch):
    monkeypatch.setattr(c, "_bot_viewer_ids", lambda: set())
    c._VIEWERS.clear()
    assert c._record_viewer(_req("10.0.0.5"), "k" + "a1" * 8) == 1          # a signed-in client
    assert c._record_viewer(_req("10.0.0.5"), "a" + "x7k2m9q0lz8w") == 2    # a guest's browser id
    for junk in ("probe-not-a-person", "k" + "Z" * 16, "a!", "<script>"):
        assert c._record_viewer(_req("10.0.0.5"), junk) == 2, junk          # private address: nobody
    assert c._record_viewer(_req("41.0.0.9"), "made-up") == 3, "a junk id from a public address is that address"
    assert c._record_viewer(_req("41.0.0.9"), "also-made-up") == 3
    c._VIEWERS.clear()
