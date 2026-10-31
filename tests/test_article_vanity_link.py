"""A SHARED ARTICLE HAS A SHORT, HUMAN-READABLE LINK: poster.place/@<name>/<title-slug>.

"can we add vanity urls for sharing nostr articles" / "entire point is for it to be short as possible, human readable"
(2026-10-30). The client MINTS the link (app.js `_articleSlug`, from the article reader's copy-link button) and the
server RESOLVES it (article_share.slug) for the preview card -- so the two slug rules are run side by side on the same
titles, and the route is run against a relay stub holding two articles.
"""
import asyncio
import json
import re
import subprocess
from pathlib import Path

from tests.test_article_link_preview import _request

ROOT = Path(__file__).resolve().parents[1]
TITLES = ["Private, and still yours: why we built PosterChanOS instead of telling you to use Tails", "PBS",
          "Hello   World!!", "A very long title that keeps going well past forty characters easily",
          "Release notes — October", "C++ | tips", "日本語のタイトル", "", "x - y"]


def _js_slugs():
    src = (ROOT / "static/js/client/app.js").read_text()
    m = re.search(r"  function _articleSlug\(title, limit=40\)\{.*?\n  \}\n", src, re.S)
    assert m, "_articleSlug not found in app.js"
    out = subprocess.run(["node", "-e", m.group(0) + "console.log(JSON.stringify(%s.map(t=>_articleSlug(t))))" % json.dumps(TITLES)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_client_and_server_slug_titles_identically_and_short():
    from app.services.article_share import slug
    assert [slug(t) for t in TITLES] == _js_slugs()
    assert slug(TITLES[0]) == "private-and-still-yours"
    assert all(len(slug(t)) <= 40 for t in TITLES)


def test_the_vanity_route_resolves_the_title_slug_and_the_d_tag(monkeypatch):
    import app.main as m
    from app.routers import client
    from app.services import article_share, git_share, nostr_store
    pk = "ab" * 32
    arts = [{"pubkey": pk, "kind": 30023, "created_at": 2, "content": "body", "tags": [["d", "draft-6x0c6"], ["title", TITLES[0]], ["summary", "Private and Secure"]]},
            {"pubkey": pk, "kind": 30023, "created_at": 1, "content": "x", "tags": [["d", "other"], ["title", "Something else"]]}]

    async def q(port, filters, timeout=4.0, strict=False):
        f = filters[0]
        if f.get("kinds") == [0]:
            return []
        return [a for a in arts if not f.get("#d") or (a["tags"][0][1] in f["#d"])]
    monkeypatch.setattr(nostr_store, "_ws_query", q)
    monkeypatch.setattr(git_share, "resolve_owner", lambda n: pk if n == "verita84" else None)
    article_share._cache.clear()
    seen = {}

    async def shell(request, db, meta=None):
        seen["meta"] = meta
        return "SHELL"
    monkeypatch.setattr(client, "render_client_shell", shell)
    asyncio.run(m.article_vanity_page("verita84", "private-and-still-yours", _request("/@verita84/private-and-still-yours")))
    meta = seen["meta"]
    assert meta and meta["title"].startswith("Private, and still yours") and meta["url"] == "https://poster.place/@verita84/private-and-still-yours", meta
    seen.clear()
    asyncio.run(m.article_vanity_page("verita84", "draft-6x0c6", _request("/@verita84/draft-6x0c6")))
    assert seen["meta"]["title"].startswith("Private, and still yours"), "the d tag must still resolve"
    seen.clear()
    asyncio.run(m.article_vanity_page("verita84", "no-such-article", _request("/@verita84/no-such-article")))
    assert seen["meta"] is None, "an unknown article still serves the app, with no card"


def test_the_client_routes_the_path_and_the_reader_copies_the_short_link():
    src = (ROOT / "static/js/client/app.js").read_text()
    assert "return { kind:'article', owner: seg[0].slice(1), d: seg[1] }" in src
    assert "e.kind==='article'" in src and "_openArticleByName(pk" in src
    disc = (ROOT / "static/js/client/discover.js").read_text()
    assert "_lk=await _articleLink(e)" in disc


# ── /a/<slug>: the shortest form, owned by the FIRST member article to claim it ─────────────────────────────────
ALICE, BOB, EVE = "a1" * 32, "b2" * 32, "e3" * 32      # alice and bob are members; eve is not


def _claims_fixture(monkeypatch, arts):
    from app.services import article_share, nostr_store, nip05_access

    async def q(port, filters, timeout=4.0, strict=False):
        f = filters[0]
        if f.get("kinds") == [0]:
            return []
        out = [a for a in arts if a["pubkey"] in (f.get("authors") or [a["pubkey"]])]
        if f.get("#d"):
            out = [a for a in out if a["tags"][0][1] in f["#d"]]
        return out
    monkeypatch.setattr(nostr_store, "_ws_query", q)
    monkeypatch.setattr(nip05_access, "granted_pubkeys", lambda: frozenset({ALICE, BOB}))
    article_share._claims.clear()
    article_share._cache.clear()
    return article_share


def _art(pk, d, title, published, created=None, eid=None):
    return {"pubkey": pk, "kind": 30023, "id": eid or (pk[:2] + d).ljust(64, "0"), "created_at": created or published + 500,
            "content": "x", "tags": [["d", d], ["title", title], ["published_at", str(published)]]}


def test_the_first_member_to_claim_a_slug_keeps_it(monkeypatch):
    t = "Private, and still yours: part two"
    arts = [_art(BOB, "b-copy", t, published=2000, created=9999),        # a later copycat, edited most recently
            _art(ALICE, "a-orig", t, published=1000, created=1500),      # the first claim
            _art(EVE, "e-old", t, published=10)]                         # older still, but not a member
    a = _claims_fixture(monkeypatch, arts)
    ref = asyncio.run(a.short_claim(3052, "private-and-still-yours"))
    assert ref == {"kind": 30023, "pubkey": ALICE, "d": "a-orig"}, ("first claim among members must win", ref)
    assert asyncio.run(a.short_claim(3052, "PRIVATE-AND-STILL-YOURS")) == ref
    assert asyncio.run(a.short_claim(3052, "../etc/passwd")) is None
    assert asyncio.run(a.short_claim(3052, "nothing-by-that-name")) is None


def test_the_short_route_renders_the_claimed_articles_card(monkeypatch):
    import app.main as m
    from app.routers import client
    a = _claims_fixture(monkeypatch, [_art(ALICE, "a-orig", "Private, and still yours: why", published=1000)])
    seen = {}

    async def shell(request, db, meta=None):
        seen["meta"] = meta
        return "SHELL"
    monkeypatch.setattr(client, "render_client_shell", shell)
    asyncio.run(m.article_short_page("private-and-still-yours", _request("/a/private-and-still-yours")))
    assert seen["meta"]["url"] == "https://poster.place/a/private-and-still-yours" and seen["meta"]["title"].startswith("Private"), seen
    r = asyncio.run(client.article_slug("private-and-still-yours"))
    assert json.loads(r.body) == {"ok": True, "pubkey": ALICE, "d": "a-orig"}
    assert json.loads(asyncio.run(client.article_slug("nope")).body) == {"ok": False}


def test_the_client_mints_a_short_only_when_the_server_says_it_is_this_article():
    src = (ROOT / "static/js/client/app.js").read_text()
    assert "r.pubkey === ev.pubkey && r.d === d) return o + '/a/' + tail" in src
    assert "kind:'article-short'" in src and "e.kind==='article-short'" in src
