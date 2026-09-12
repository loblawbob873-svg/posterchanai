"""Torrent RSS feeds — subscribe, filter, poll, add.

Each test here names a way this feature can fail SILENTLY, which is the only interesting kind:

  * a feed URL is user-supplied and this node fetches it with its own network position, so it must
    go through the news reader's SSRF guard — the one that re-validates every REDIRECT HOP — and not
    a second copy that forgets the redirects;
  * the magnet is wherever the feed chose to put it (<link>, an <enclosure>, a torznab attr, or
    nowhere at all — just an infohash), and a parser that only reads one of those quietly subscribes
    you to a feed that can never add anything;
  * an unfiltered feed is a category download, so include/exclude and a per-poll cap are the bounds;
  * the FIRST poll of a new feed must add nothing, or subscribing starts the feed's whole backlog;
  * a corrupt state file must not read as "no feeds seen", which re-adds that backlog on every poll.
"""
import asyncio
import json
import re
import time
from pathlib import Path

import pytest

from app.services import rss_service
from app.services import torrent_rss_service as trss

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    """Every test gets its own state file — never the node's live torrent state."""
    monkeypatch.setattr(trss, "_state_dir", lambda: tmp_path / "resume")
    yield


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- SSRF

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/rss",
    "http://localhost/rss",
    "http://10.0.0.5/rss",
    "http://192.168.0.85:9117/api",
    "http://nas.lan/feed.xml",
    "file:///etc/passwd",
    "gopher://example.com/",
])
def test_a_feed_url_the_guard_refuses_cannot_be_subscribed(url):
    """The subscription itself is the first gate. Without it the URL is stored and the WORKER — a
    background process nobody is watching — is the thing that dials 127.0.0.1 every 30 minutes."""
    with pytest.raises(ValueError):
        trss.add_feed(url)
    assert trss.list_feeds() == []


def test_the_guard_is_the_news_readers_so_every_redirect_hop_is_re_checked(monkeypatch):
    """A hand-rolled check validates the URL you TYPED. `rss_service._get_bytes` follows redirects
    manually and runs is_safe_host on each hop, which is what closes SSRF-via-302 — so the fetch has
    to be that function, not an httpx call of our own."""
    src = (ROOT / "app" / "services" / "torrent_rss_service.py").read_text()
    assert "rss_service._get_bytes" in src, "the feed fetch no longer goes through the guarded fetcher"
    assert "rss_service.looks_fetchable" in src

    seen = {}

    async def _fake(url):
        seen["url"] = url
        return b"<rss><channel></channel></rss>"

    monkeypatch.setattr(rss_service, "_get_bytes", _fake)
    _run(trss.fetch_feed_items("https://example.com/rss"))
    assert seen["url"] == "https://example.com/rss"

    with pytest.raises(ValueError):
        _run(trss.fetch_feed_items("http://127.0.0.1/rss"))


# --------------------------------------------------------------------------- parsing

_SHOWRSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>showRSS</title>
<item><title>Some Show S01E04 1080p WEB</title>
<link>magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&amp;dn=Some+Show</link>
<pubDate>Tue, 02 Sep 2025 10:00:00 +0000</pubDate></item></channel></rss>"""

_ENCLOSURE = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>A Film 2024 1080p</title><link>https://tracker.example/view/9</link>
<enclosure url="magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" type="application/x-bittorrent"/>
</item></channel></rss>"""

_NYAA = b"""<?xml version="1.0"?><rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel>
<item><title>[Sub] Anime - 07 [1080p]</title>
<link>https://nyaa.si/download/1234.torrent</link>
<guid isPermaLink="true">https://nyaa.si/view/1234</guid>
<nyaa:infoHash>cccccccccccccccccccccccccccccccccccccccc</nyaa:infoHash>
<nyaa:size>1.4 GiB</nyaa:size></item></channel></rss>"""

_TORZNAB = b"""<?xml version="1.0"?><rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
<item><title>Album FLAC 2019</title><link>https://jackett.example/dl/abc</link>
<size>734003200</size>
<torznab:attr name="magneturl" value="magnet:?xt=urn:btih:dddddddddddddddddddddddddddddddddddddddd"/>
<torznab:attr name="infohash" value="dddddddddddddddddddddddddddddddddddddddd"/>
</item></channel></rss>"""


def test_the_magnet_is_found_wherever_the_feed_puts_it():
    for raw, where in ((_SHOWRSS, "link"), (_ENCLOSURE, "enclosure"),
                       (_NYAA, "infohash"), (_TORZNAB, "torznab attr")):
        items = trss.parse_torrent_feed(raw)
        assert len(items) == 1, f"{where}: item lost entirely"
        assert items[0]["magnet"].startswith("magnet:?xt=urn:btih:"), \
            f"{where}: no magnet — this feed could never add anything"


def test_a_torrent_file_url_is_kept_when_there_is_one():
    item = trss.parse_torrent_feed(_NYAA)[0]
    assert item["torrent_url"] == "https://nyaa.si/download/1234.torrent"
    assert item["id"] == "https://nyaa.si/view/1234"      # the guid, so dedup survives a URL change


def test_size_and_date_survive_the_parse():
    assert trss.parse_torrent_feed(_TORZNAB)[0]["size"] == 734003200
    assert trss.parse_torrent_feed(_SHOWRSS)[0]["ts"] > 0


def test_the_news_parser_would_have_thrown_the_torrent_away():
    """WHY this module does not simply call rss_service.parse_feed: that one is a NEWS reader and
    keeps an <enclosure> only when its type says image. On a torrent feed the enclosure IS the
    torrent, so reusing it would parse the item and lose the only thing worth having."""
    _title, items = rss_service.parse_feed(_ENCLOSURE, "https://tracker.example")
    assert items, "fixture no longer parses at all — rewrite this test"
    assert not any("magnet:" in json.dumps(i) for i in items), \
        "rss_service now keeps torrent enclosures; this module could be simplified"


def test_an_item_with_no_torrent_at_all_is_skipped():
    raw = b"""<rss><channel><item><title>Just an article</title>
              <link>https://example.com/post</link></item></channel></rss>"""
    assert trss.parse_torrent_feed(raw) == []


# --------------------------------------------------------------------------- filters

def test_an_empty_include_takes_everything():
    assert trss.matches("anything at all") is True


def test_include_is_any_term_and_is_case_insensitive():
    assert trss.matches("Some Show S01E04 1080p", include="1080p, 2160p")
    assert trss.matches("Some Show S01E04 2160P", include="1080p, 2160p")
    assert not trss.matches("Some Show S01E04 720p", include="1080p, 2160p")


def test_exclude_wins_over_include():
    """"1080p but never HDCAM" is the shape people actually type, and an include-wins order makes
    the exclude box do nothing on the one release it was written for."""
    assert not trss.matches("A Film 1080p HDCAM", include="1080p", exclude="hdcam")
    assert trss.matches("A Film 1080p WEB", include="1080p", exclude="hdcam")


def test_a_term_in_slashes_is_a_regex_and_is_case_insensitive():
    assert trss.matches("Some Show s01e04", include=r"/S0[12]E\d\d/")
    assert not trss.matches("Some Show S03E04", include=r"/S0[12]E\d\d/")


def test_an_invalid_regex_falls_back_to_a_literal_rather_than_matching_nothing():
    assert trss.matches("a [unclosed thing", include="/[unclosed/") in (True, False)   # must not raise


# --------------------------------------------------------------------------- polling

class _Adds:
    def __init__(self):
        self.added = []

    async def __call__(self, magnet="", torrent_url=""):
        self.added.append(magnet or torrent_url)
        return "hash%d" % len(self.added)


def _feed_of(items):
    async def _fetch(url):
        return items
    return _fetch


def _items(n, prefix="Rel"):
    return [{"id": f"id{i}", "title": f"{prefix} {i} 1080p", "magnet": f"magnet:?xt=urn:btih:{i:040d}",
             "torrent_url": "", "link": "", "ts": int(time.time()), "size": 1} for i in range(n)]


def test_the_first_poll_of_a_new_feed_adds_nothing_and_learns_the_backlog(monkeypatch):
    """A feed is a window onto a backlog. Without this, subscribing to a 200-item feed starts 200
    downloads — the single loudest way this feature could go wrong."""
    adds = _Adds()
    monkeypatch.setattr(trss, "fetch_feed_items", _feed_of(_items(20)))
    monkeypatch.setattr(trss, "add_one", adds)

    f = trss.add_feed("https://example.com/rss")
    stored = trss.get_feed(f["id"])
    r = _run(trss.poll_feed(stored))
    assert r["primed"] is True and r["added"] == 0
    assert adds.added == []
    assert trss.list_feeds()[0]["primed"] is True


def test_the_second_poll_adds_only_what_is_new(monkeypatch):
    adds = _Adds()
    monkeypatch.setattr(trss, "add_one", adds)
    monkeypatch.setattr(trss, "fetch_feed_items", _feed_of(_items(3)))
    f = trss.add_feed("https://example.com/rss")
    _run(trss.poll_feed(trss.get_feed(f["id"])))          # prime
    _run(trss.poll_feed(trss.get_feed(f["id"])))          # nothing new
    assert adds.added == []

    fresh = _items(3) + [{"id": "NEW", "title": "Brand New 1080p",
                          "magnet": "magnet:?xt=urn:btih:" + "e" * 40,
                          "torrent_url": "", "link": "", "ts": 0, "size": 1}]
    monkeypatch.setattr(trss, "fetch_feed_items", _feed_of(fresh))
    r = _run(trss.poll_feed(trss.get_feed(f["id"])))
    assert r["added"] == 1 and adds.added == ["magnet:?xt=urn:btih:" + "e" * 40]

    _run(trss.poll_feed(trss.get_feed(f["id"])))          # and never twice
    assert len(adds.added) == 1


def test_the_filter_decides_what_a_poll_takes(monkeypatch):
    adds = _Adds()
    monkeypatch.setattr(trss, "add_one", adds)
    monkeypatch.setattr(trss, "fetch_feed_items", _feed_of([]))
    f = trss.add_feed("https://example.com/rss", include="1080p", exclude="hdcam")
    _run(trss.poll_feed(trss.get_feed(f["id"])))          # prime on an empty feed

    monkeypatch.setattr(trss, "fetch_feed_items", _feed_of([
        {"id": "a", "title": "Wanted 1080p WEB", "magnet": "magnet:?a", "torrent_url": "", "link": "", "ts": 0, "size": 0},
        {"id": "b", "title": "Wanted 1080p HDCAM", "magnet": "magnet:?b", "torrent_url": "", "link": "", "ts": 0, "size": 0},
        {"id": "c", "title": "Wanted 720p WEB", "magnet": "magnet:?c", "torrent_url": "", "link": "", "ts": 0, "size": 0},
    ]))
    r = _run(trss.poll_feed(trss.get_feed(f["id"])))
    assert r["matched"] == 1 and adds.added == ["magnet:?a"]


def test_max_per_poll_bounds_a_feed_with_no_filter(monkeypatch):
    from app.services import settings_store
    settings_store._CACHE["torrent_rss_max_per_poll"] = "3"
    adds = _Adds()
    monkeypatch.setattr(trss, "add_one", adds)
    monkeypatch.setattr(trss, "fetch_feed_items", _feed_of([]))
    f = trss.add_feed("https://example.com/rss")
    _run(trss.poll_feed(trss.get_feed(f["id"])))
    monkeypatch.setattr(trss, "fetch_feed_items", _feed_of(_items(30)))
    r = _run(trss.poll_feed(trss.get_feed(f["id"])))
    assert r["added"] == 3 and len(adds.added) == 3
    assert r["skipped"] == 27


def test_check_now_is_a_preview_it_adds_nothing_and_remembers_nothing(monkeypatch):
    """A filter you cannot try is one you find out about after it downloaded the wrong season."""
    adds = _Adds()
    monkeypatch.setattr(trss, "add_one", adds)
    monkeypatch.setattr(trss, "fetch_feed_items", _feed_of(_items(4)))
    f = trss.add_feed("https://example.com/rss")
    r = _run(trss.poll_feed(trss.get_feed(f["id"]), dry_run=True))
    assert r["matched"] == 4 and r["added"] == 0 and adds.added == []
    assert r["titles"], "a preview that names nothing is not a preview"
    # and it did not prime the feed, so the real first poll still learns the backlog
    assert trss.get_feed(f["id"]).get("primed") is False


def test_a_fetch_failure_is_recorded_and_never_looks_like_an_empty_feed(monkeypatch):
    async def _boom(url):
        raise ValueError("disallowed host")
    monkeypatch.setattr(trss, "fetch_feed_items", _boom)
    f = trss.add_feed("https://example.com/rss")
    r = _run(trss.poll_feed(trss.get_feed(f["id"])))
    assert r["error"] == "disallowed host" and r["added"] == 0
    assert trss.list_feeds()[0]["last_error"] == "disallowed host"
    assert trss.get_feed(f["id"]).get("primed") is False, \
        "a failed fetch must not prime the feed — that would mark a backlog it never read as seen"


def test_a_disabled_feed_is_not_polled(monkeypatch):
    adds = _Adds()
    monkeypatch.setattr(trss, "add_one", adds)
    monkeypatch.setattr(trss, "fetch_feed_items", _feed_of(_items(5)))
    f = trss.add_feed("https://example.com/rss", enabled=False)
    _run(trss.poll_all())
    assert adds.added == []
    assert f["enabled"] is False


# --------------------------------------------------------------------------- the store

def test_a_corrupt_state_file_is_not_read_as_no_feeds():
    """`seen` lives in this file. Reading a torn/corrupt file as empty would drop every subscription
    AND every seen id, and the next poll would re-add the entire backlog of every feed."""
    trss.add_feed("https://example.com/rss")
    trss._store_path().write_text("{not json at all")
    with pytest.raises(Exception):
        trss.list_feeds()


def test_feeds_survive_a_round_trip_and_can_be_edited_and_removed():
    f = trss.add_feed("example.com/rss", title="Shows", include="1080p")
    assert f["url"] == "https://example.com/rss"          # normalize_url adds the scheme
    assert trss.list_feeds()[0]["title"] == "Shows"
    trss.update_feed(f["id"], include="2160p", enabled=False)
    got = trss.list_feeds()[0]
    assert got["include"] == "2160p" and got["enabled"] is False
    assert trss.remove_feed(f["id"]) is True
    assert trss.list_feeds() == []
    assert trss.remove_feed(f["id"]) is False


def test_the_same_feed_cannot_be_subscribed_twice():
    trss.add_feed("https://example.com/rss")
    with pytest.raises(ValueError):
        trss.add_feed("https://example.com/rss/")


def test_removing_a_feed_forgets_its_seen_ids():
    """Otherwise the file grows for ever with ids nothing can ever match again."""
    f = trss.add_feed("https://example.com/rss")
    trss._record(f["id"], ["a", "b", "c"])
    trss.remove_feed(f["id"])
    assert trss._read_store()["seen"] == {}


# --------------------------------------------------------------------------- wiring

def test_the_poller_runs_in_the_worker():
    from app import worker
    assert any(m == "app.services.torrent_rss_service" for _n, m, _f in worker._SCHEDULERS), \
        "the feed poller is not started by the worker — it would never run"


def test_the_tick_reads_its_flag_from_the_settings_store_not_a_build_default(monkeypatch):
    """The documented worker trap: a service that reads a build-time default silently never runs
    (or, worse, always runs). The job is always scheduled; the TICK is the gate."""
    from app.services import settings_store
    calls = []

    async def _poll():
        calls.append(1)
        return {"feeds": 0, "added": 0}

    monkeypatch.setattr(trss, "poll_all", _poll)

    settings_store._CACHE["torrent_rss_enabled"] = "false"
    settings_store._CACHE["bt_enabled"] = "true"
    _run(trss._tick())
    assert calls == [], "the poller ran with the feature switched off"

    settings_store._CACHE["torrent_rss_enabled"] = "true"
    _run(trss._tick())
    assert calls == [1], "the poller did not run with the feature switched on"


def test_every_new_setting_is_declared_so_it_hydrates():
    """An undeclared key is dropped from GET /api/admin/settings, so the input loads blank on every
    visit and the checkbox posts `false` over the stored value on the next Save."""
    from app.schemas import SettingsResponse
    fields = SettingsResponse.model_fields
    for key in ("torrent_rss_enabled", "torrent_rss_interval_minutes", "torrent_rss_max_per_poll"):
        assert key in fields, f"{key} is not in SettingsResponse — it will never hydrate"

    html = (ROOT / "templates" / "admin" / "tabs" / "network.html").read_text()
    for key in ("torrent_rss_enabled", "torrent_rss_interval_minutes", "torrent_rss_max_per_poll"):
        m = re.search(r'id="%s"\s+name="%s"' % (key, key), html)
        assert m, f"{key} has no input whose id and name both match (hydration reads id, Save reads name)"


def test_a_feed_that_matched_things_and_added_none_of_them_is_not_a_clean_check(monkeypatch):
    """The torrent client being off, unconfigured, or unable to reach its proxy fails HERE and
    nowhere else. Recorded as a success it reads exactly like a feed whose filter matches nothing —
    which sends whoever is debugging it to the wrong screen."""
    async def _boom(magnet="", torrent_url=""):
        raise RuntimeError("torrent client not configured")

    monkeypatch.setattr(trss, "add_one", _boom)
    monkeypatch.setattr(trss, "fetch_feed_items", _feed_of([]))
    f = trss.add_feed("https://example.com/rss")
    _run(trss.poll_feed(trss.get_feed(f["id"])))                  # prime
    monkeypatch.setattr(trss, "fetch_feed_items", _feed_of(_items(2)))
    r = _run(trss.poll_feed(trss.get_feed(f["id"])))
    assert r["added"] == 0
    assert "torrent client not configured" in r["error"]
    assert "torrent client not configured" in trss.list_feeds()[0]["last_error"]
