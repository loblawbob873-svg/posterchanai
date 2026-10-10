"""Image-search thumbnail ids: an in-process TTL cache now, not the `proxy_image_cache` table (#161)."""
import time
from unittest import mock

from app.services import proxy_image_cache as P


def test_an_id_resolves_to_its_url_for_five_minutes_and_then_not():
    sid = P.register("https://example.com/a.jpg")
    assert P.get(sid) == "https://example.com/a.jpg"
    with mock.patch.object(time, "time", lambda: P._ids[sid][1] + 1):
        assert P.get(sid) is None, "an expired id still resolved"
    assert P.get("nope") is None and P.get("") is None


def test_the_old_call_signature_still_works_and_needs_no_database():
    sid = P.register("https://example.com/b.jpg", object())      # `db` is ignored
    assert P.get(sid, object()) == "https://example.com/b.jpg"


def test_it_is_bounded():
    with mock.patch.object(P, "_MAX", 50):
        for i in range(200):
            P.register("https://example.com/%d.jpg" % i)
        assert len(P._ids) <= 50


def test_an_empty_url_is_refused():
    import pytest
    with pytest.raises(ValueError):
        P.register("  ")
