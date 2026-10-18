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


def test_a_loop_of_invented_browser_ids_is_not_a_crowd(monkeypatch):
    """A valid-looking guest id is random and free to invent: `?v=a<random>` in a loop added a person per request
    (security review 2026-10-18). One address may claim a few (a household); beyond that it is that address."""
    import random
    import string
    monkeypatch.setattr(c, "_bot_viewer_ids", lambda: set())
    c._VIEWERS.clear()
    getattr(c, "_VIEWER_ADDR", {}).clear()
    r = random.Random(4)
    n = 0
    for _ in range(500):
        n = c._record_viewer(_req("41.0.0.77"), "a" + "".join(r.choice(string.ascii_lowercase) for _ in range(12)))
    assert n <= 5, "%d people from one address" % n
    assert c._record_viewer(_req("41.0.0.78"), "a" + "q" * 12) == n + 1, "another address is still a person"
    c._VIEWERS.clear()
    getattr(c, "_VIEWER_ADDR", {}).clear()
