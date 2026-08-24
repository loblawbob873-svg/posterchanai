"""Profiles derive their joined date from relay history, never the recent page cache."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


def test_profile_searches_historical_relay_coverage_for_joined_date():
    body = APP[APP.index("async function renderProfileView(pk){"):
               APP.index("function editProfile", APP.index("async function renderProfileView(pk){"))]
    assert "_seenEvents.reduce" not in body
    assert "_nostrFirstSeen(pk)" in body
    assert "authors:[pk],until,limit:1" in APP
    assert "r.complete===false" in APP
    assert "Joined Nostr" in body


def test_joined_date_is_hidden_until_historical_lookup_succeeds():
    assert 'id="prof-joined" hidden' in APP
    assert ".prof-joined[hidden]{display:none}" in CSS
    assert "el.hidden=false" in APP


def test_joined_date_is_cached_but_periodically_rechecked_for_older_history():
    assert "pc_first_seen_v2_" in APP
    assert "ttl=30*86400" in APP
