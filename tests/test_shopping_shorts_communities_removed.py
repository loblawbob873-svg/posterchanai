"""Shopping, Shorts and Communities are gone — and three things beside them are NOT.

Removed 2026-09-04 on the owner's instruction: the Shopping screen (NIP-99 classified listings,
kind 30402/30403), Discover -> Shorts (Divine's kind 34236 + NIP-71 kind 22) and Communities
(NIP-72 moderated communities, kind 34550). Each went out whole — renderer, cards, editor, nav
entry, launcher tile, CSS, translations and the compose path that posted into a community — not
just its entry point.

WHAT STAYED, and why a "remove every reference" grep must not land on it:

1. **The relay keeps ingesting and serving 30402 / 34550 / 34236.** Removing a CLIENT screen is not
   a decision about everybody else's events. This node's relay serves gitworkshop, Amethyst, Divine
   and every other reader on it, and `backfill_author` is what restores a member's own history —
   including listings, communities and shorts they published from another client. Dropping the
   kinds there would silently hole a restore and stop serving readers who never asked for this
   change. `tests/test_user_data_sync_coverage.py` owns that assertion; this file only pins that
   nobody quietly deleted them while deleting the screens.

2. **`_backfillPostFolder`'s kind list still names 30402 / 34236.** It is a RECOVERY over the
   account's already-published history — it finds media a pre-Files-index composer uploaded and
   files it under "Posts". A user who posted a listing or a short before today still owns those
   blobs, and narrowing the query would silently stop recovering them. It publishes nothing.

3. **"Communities" is still a string in the client, and it is CONCORD's.** Direct messages and
   Communities are the two tabs inside ONE Messages window (`#messages-communities` ->
   `switchView('concord')`). Concord is a different product, the owner uses it, and the word
   collides exactly. Same trap on the desktop: `communityStats()` and os.js's `community: true`
   widget are the sidebar's web-of-trust / who-is-online counters, not NIP-72.
"""
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


APP = _read("static/js/client/app.js")
STORE = _read("static/js/client/store.js")
CSS = _read("static/css/client.css")
HTML = _read("templates/client.html")


# ---------------------------------------------------------------- gone


@pytest.mark.parametrize("path", [
    "tests/client/test_shorts.py",
    "tests/client/test_community_timeline_navigation.py",
    "scripts/check_shorts_mobile.py",
])
def test_the_dead_tests_and_checks_are_deleted(path):
    """The suite DISCOVERS scripts/check_*.py — one left behind runs and fails."""
    assert not os.path.exists(os.path.join(ROOT, path)), path


def test_no_check_script_is_still_registered():
    assert "check_shorts_mobile" not in _read("scripts/checkall.py")


@pytest.mark.parametrize("view", ["market", "shorts", "communities"])
def test_the_sidebar_has_no_entry(view):
    """os.js reads the desktop's app list off the sidebar, so this also removes the desktop icon
    and the start-menu row — there is no second list to edit."""
    assert 'data-view="%s"' % view not in HTML


@pytest.mark.parametrize("view", ["market", "shorts", "communities"])
def test_the_router_has_no_dead_view(view):
    """A view left in the router with no way to reach it is dead code."""
    assert "VIEW==='%s'" % view not in APP


@pytest.mark.parametrize("fn", [
    "renderMarket", "marketCard", "openListing", "publishListing", "renderListingEditor",
    "toggleListingSold", "deleteListing", "mktPriceTag", "mktStatus", "mktImages", "mktCats",
    "renderShorts", "_shortsGrid", "_shortsPlayer", "_shortTagsFor",
    "renderCommunities", "communityCard", "openCommunity", "communityPostTags",
])
def test_the_renderers_are_gone(fn):
    """A nav entry removed while the renderer stays is a feature nobody can find."""
    assert fn not in APP


@pytest.mark.parametrize("view", ["market", "shorts", "communities"])
def test_no_launcher_tile_on_android(view):
    tiles = _read("mobile/android/app/src/main/java/place/poster/app/home/HomeTiles.java")
    assert 'new Tile("%s"' % view not in tiles


@pytest.mark.parametrize("cls", [
    ".mkt-", ".shorts-", ".short-tile", ".short-card", ".community-card", ".listing-view",
])
def test_the_stylesheet_lost_the_blocks(cls):
    assert cls not in CSS


def test_compose_can_no_longer_be_opened_on_a_community():
    """The composer's `community` parameter published a NIP-72 kind-1111 into a community. The
    ARTICLE-COMMENT half of that same branch is untouched — it is a different feature that merely
    shared the code path."""
    sig = APP[APP.index("function compose({"):]
    sig = sig[:sig.index("\n")]
    assert "community" not in sig, sig
    assert "articleComment" in sig
    assert "articleCommentTags(" in APP


def test_the_timeline_cannot_be_handed_a_community_definition():
    """Structural, not a filter: 34550 is out of Store.feed()'s kind allowlist, so a community
    definition arriving over the firehose can never reach a timeline draw. The old guard was one
    `ev.kind!==34550` in _tlFilter, which is the shape that rots."""
    feed = STORE[STORE.index("    feed(filterFn){"):]
    feed = feed[:feed.index("byKind(kind)")]
    assert "34550" not in feed


@pytest.mark.parametrize("lang", ["en", "ar", "ja"])
@pytest.mark.parametrize("key", [
    "Shopping", "draft saved (in Shopping)", "Draft listings", "Post to this community",
    "posted to community", "No posts in this community yet.",
])
def test_the_translations_lost_the_dead_strings(lang, key):
    cat = json.loads(_read("static/i18n/%s.json" % lang))
    assert key not in cat


# ---------------------------------------------------------------- stayed


@pytest.mark.parametrize("kind", [30402, 34550, 34236])
def test_the_relay_still_ingests_the_kinds_for_everyone_else(kind):
    """Removing a client screen is not a decision about other people's events. This node's relay
    still stores and serves them, and a member restore still brings back what they published from
    another client."""
    from app.services.nostr_relay import ingest
    import inspect

    src = inspect.getsource(ingest.backfill_author)
    backfill = {int(k) for k in re.search(r"kinds = kinds or \[([0-9,\s]+)\]", src)
                .group(1).replace("\n", "").split(",") if k.strip()}
    assert kind in backfill

    thread = _read("app/services/nostr_relay/thread.py")
    default = re.search(r'nostr_relay_ingest_kinds", "([0-9,]+)"', thread).group(1)
    assert kind in {int(k) for k in default.split(",")}


def test_the_files_index_repair_still_recovers_listing_and_short_media():
    """A user who posted a listing or a short BEFORE today still owns those blobs; this read-only
    recovery is what files them under "Posts". Narrowing its kinds would silently stop recovering
    media for events that already exist. It publishes nothing."""
    fn = APP[APP.index("function _backfillPostFolder(list){"):][:1200]
    assert "kinds:[1,20,30023,30402,34235,34236]" in fn
    assert "publish(" not in fn


def test_concords_communities_tab_is_untouched():
    """Concord is a DIFFERENT product and the word collides exactly: Direct messages and
    Communities are the two tabs inside one Messages window."""
    assert 'id="messages-communities"' in APP
    assert "switchView('concord')" in APP
    assert '"Communities"' in _read("static/i18n/en.json"), "the tab's label must still translate"
    assert 'class="messages-tabs"' in _read("static/js/client/concord.js")


def test_the_web_of_trust_counters_are_untouched():
    """`communityStats` is the sidebar's network-size / who-is-online panel and the desktop's
    Community widget — nothing to do with NIP-72."""
    assert "communityStats" in APP
    assert "community: true" in _read("static/js/client/os.js")


def test_the_raw_event_viewer_can_still_name_the_kinds():
    """A generic kind -> name dictionary for "view raw". It already names kinds this client never
    implemented (30818 wiki, 30008 profile badges), and these events still arrive over the
    firehose, so a reader must not be shown a bare number."""
    assert "30402:'classified listing'" in APP
    assert "34550:'community definition'" in APP
