"""Bad Post of the Day -- the bot that announces the most-voted post of each voting window.

"users can vote for their 'Bad Post of the Day' by clicking on post hamburger menu ... the bot can make a
post with the screenshot of the post with the highest votes and make a mean Ai-generated post about it",
then: every 4 hours (configurable), counting only the votes cast in that window, tagging the author.

What is checked, as the community sees it:
  * the count: one vote per voter per post, a vote outside the window or for your own post is not a vote,
    the most-voted post wins, a window with no votes posts nothing;
  * the schedule: 4-hour windows from local midnight, announced when they close, the interval comes from
    the bot's Admin config, and switching the bot on does not announce the past day;
  * "could not ask the relay" posts nothing AND leaves that window open, so it is announced once the relay
    answers -- never skipped for ever and never read as "no votes";
  * a post is announced once, ever -- not again in the same window, not again when it is voted up later;
  * the published Nostr event TAGS the author (`p` tag + `nostr:npub` in the text), carries the screenshot
    and the facepalm, and congratulates them on the prize;
  * the roast is the model's only when it passes the guard (no names, slurs, protected traits, threats);
  * the relay side: only well-formed `pcai.badpost` labels count, never your own post, never a bot's.
"""
import asyncio
import base64
import datetime
import os
import sys

import pytest

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "botframework")
POST, POST2, POST3 = "a" * 64, "b" * 64, "c" * 64
AUTHOR = "d4" * 32
V = ["%064x" % i for i in range(1, 10)]


def _vote(voter, post=POST, at=0, author=AUTHOR):
    return {"id": f"{voter[:8]}{post[:8]}{at}", "voter": voter, "post": post, "author": author, "at": at}


@pytest.fixture
def bot(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(HERE)
    monkeypatch.delenv("BADPOST_INTERVAL_HOURS", raising=False)
    for m in ("nostr_badpostbot", "community_api"):
        sys.modules.pop(m, None)
    import nostr_badpostbot as b
    monkeypatch.setattr(b, "_state_path", lambda: str(tmp_path / "badpost.json"))
    monkeypatch.setattr(b, "_ai_on", lambda: False)
    return b


class FakeApp:
    """/api/community/badpost-votes + /badpost-card, with a switch that makes the relay unreachable."""

    def __init__(self, b):
        self.b, self.votes, self.down, self.posted, self.asked = b, [], False, [], []

    def get(self, path, **kw):
        self.asked.append((path, kw))
        if self.down:
            raise self.b.community_api.Unavailable("relay down")
        if path == "badpost-votes":
            return {"votes": [v for v in self.votes
                              if v["at"] >= kw.get("since", 0) and (not kw.get("until") or v["at"] < kw["until"])]}
        if path == "badpost-card":
            pid = kw["id"]
            return {"post": {"id": pid, "pubkey": AUTHOR, "text": "pineapple on everything",
                             "ref": "nostr:npub1author", "note_ref": "nostr:note1" + pid[:6], "handle": "@x"},
                    "card": base64.b64encode(b"\x89PNGcard").decode(), "content_type": "image/png"}
        raise AssertionError(path)


@pytest.fixture
def app(bot, monkeypatch):
    a = FakeApp(bot)
    monkeypatch.setattr(bot.community_api, "get", a.get)
    monkeypatch.setattr(bot, "_post", lambda text, images: a.posted.append((text, images)) or {"id": "e" * 64})
    return a


def T(h, m=0, day=11):
    return datetime.datetime(2026, 10, day, h, m, 0)


def ts(h, m=0, day=11):
    return int(T(h, m, day).timestamp())


# ---- the count ------------------------------------------------------------------------------------

def test_one_vote_per_voter_and_the_most_voted_post_wins(bot):
    s, e = ts(8), ts(12)
    votes = [_vote(V[0], POST, ts(9)), _vote(V[0], POST, ts(10)), _vote(V[0], POST, ts(11)),   # one voter, thrice
             _vote(V[1], POST2, ts(9)), _vote(V[2], POST2, ts(9, 5)),
             _vote(V[3], POST3, ts(7)), _vote(V[4], POST3, ts(7)), _vote(V[5], POST3, ts(12)),  # outside the window
             _vote(AUTHOR, POST, ts(9))]                                                        # your own post
    ranked = bot.tally(votes, s, e)
    assert [(r["post"], r["votes"]) for r in ranked] == [(POST2, 2), (POST, 1)], ranked
    assert bot.tally([], s, e) == []
    assert bot.tally(votes, s, e, announced={POST2: 1})[0]["post"] == POST, "an announced post won again"


# ---- the schedule ---------------------------------------------------------------------------------

def test_windows_are_four_hours_from_local_midnight_and_announced_when_they_close(bot):
    h = bot.interval_hours()
    assert h == 4
    assert bot.window_of(T(13, 30), h) == (T(12), T(16))
    assert bot.window_of(T(0), h) == (T(0), T(4))
    assert bot.window_of(T(23, 59), h) == (T(20), T(0, day=12))
    done = T(8).timestamp()
    assert bot.due_windows(T(11, 59), done, h) == [], "a window was announced before it closed"
    assert bot.due_windows(T(12, 0), done, h) == [(T(8), T(12))]
    assert bot.due_windows(T(12, 0), T(12).timestamp(), h) == [], "a window was announced twice"
    # Switched on for the first time: the window that just closed, not the whole past day.
    assert bot.due_windows(T(13), None, h) == [(T(8), T(12))]
    # After an outage, every missed window in order -- but nothing a day old.
    assert bot.due_windows(T(13), T(0).timestamp(), h) == [(T(0), T(4)), (T(4), T(8)), (T(8), T(12))]
    assert bot.due_windows(T(13, day=13), T(0).timestamp(), h)[0][0] >= T(12, day=12)


def test_the_interval_comes_from_the_bots_admin_config(bot, monkeypatch):
    monkeypatch.setenv("BADPOST_INTERVAL_HOURS", "6")
    assert bot.interval_hours() == 6
    assert bot.window_of(T(13), 6) == (T(12), T(18))
    for raw, want in (("0", 1), ("99", 24), ("junk", 4), ("", 4)):
        monkeypatch.setenv("BADPOST_INTERVAL_HOURS", raw)
        assert bot.interval_hours() == want, raw
    import re
    js = open(os.path.join(HERE, "..", "static", "js", "admin-bots.js")).read()
    html = open(os.path.join(HERE, "..", "templates", "admin", "tabs", "bots.html")).read()
    mgr = open(os.path.join(HERE, "..", "app", "services", "bot_manager_service.py")).read()
    assert "'badpost_interval_hours'" in js and 'id="bot_f_badpost_interval_hours"' in html
    assert re.search(r"bot_ft_badpost:\s*'--badpost'", js) and 'id="bot_ft_badpost"' in html
    assert re.search(r'setif\("badpost_interval_hours",\s*"BADPOST_INTERVAL_HOURS"\)', mgr)


# ---- announcing -----------------------------------------------------------------------------------

def test_no_votes_posts_nothing_and_the_window_is_spent(bot, app):
    bot.tick(T(12, 0))
    assert app.posted == []
    assert bot._load().get("done_until") == T(12).timestamp()


def test_could_not_ask_posts_nothing_and_the_window_is_retried(bot, app):
    bot._save({"done_until": T(8).timestamp()})
    app.votes = [_vote(V[0], POST, ts(9)), _vote(V[1], POST, ts(10))]
    app.down = True
    bot.tick(T(12, 0))
    bot.tick(T(12, 1))
    assert app.posted == [], "posted while the relay could not be asked"
    assert bot._load().get("done_until") == T(8).timestamp(), "an unreadable window was marked done"
    app.down = False
    bot.tick(T(12, 2))
    assert len(app.posted) == 1 and "2 votes" in app.posted[0][0], "the window was skipped, not retried"
    assert bot._load().get("done_until") == T(12).timestamp()


def test_a_post_is_announced_once_ever(bot, app):
    bot._save({"done_until": T(8).timestamp()})
    app.votes = [_vote(V[0], POST, ts(9)), _vote(V[1], POST, ts(9)), _vote(V[2], POST2, ts(9))]
    bot.tick(T(12, 0))
    bot.tick(T(12, 1))
    bot._save({**bot._load(), "done_until": T(8).timestamp()})      # even if the window comes round again
    bot.tick(T(12, 2))
    texts = [t for t, _ in app.posted]
    assert len(texts) == 2 and "nostr:note1" + POST[:6] in texts[0] and "nostr:note1" + POST2[:6] in texts[1], texts
    # The next window: the old winner voted up again is never a candidate; nobody else -> nothing.
    app.votes += [_vote(V[3], POST, ts(13)), _vote(V[4], POST, ts(14))]
    bot.tick(T(16, 0))
    assert len(app.posted) == 2, "a post was announced twice"


def test_the_announcement_tags_the_author_and_carries_both_pictures(bot, app):
    bot._save({"done_until": T(8).timestamp()})
    app.votes = [_vote(V[0], POST, ts(9)), _vote(V[1], POST, ts(10)), _vote(V[2], POST, ts(11))]
    bot.tick(T(12, 0))
    (text, images), = app.posted
    assert "Congratulations nostr:npub1author, you've won Bad Post of the Day with 3 votes!" in text, text
    assert text.endswith("nostr:note1" + POST[:6])
    assert images[0] == (b"\x89PNGcard", "image/png"), "the screenshot of the post is missing"
    assert images[1][1] == "image/png" and images[1][0][:8] == b"\x89PNG\r\n\x1a\n", "the facepalm is missing"
    assert [k for k, _ in app.asked].count("badpost-votes") == 1
    assert ("badpost-votes", {"since": ts(8), "until": ts(12)}) in app.asked, app.asked


def test_the_published_event_carries_the_authors_p_tag(bot, monkeypatch):
    """Through the REAL post_note: the `nostr:npub` mention must become a `p` tag, or nobody is notified."""
    monkeypatch.setenv("NOSTR_NSEC", "11" * 32)
    for m in ("nostr", "config"):
        sys.modules.pop(m, None)
    import nostr
    from app.services.nostr import bech32, nostr_service
    author = "7e" * 32
    got = []

    async def no_media(seckey, text, media_list, cfg):
        return text, [["imeta", "url https://x/" + str(i)] for i, _ in enumerate(media_list)]

    async def publish(relays, ev, direct=False):
        got.append((relays, ev))
        return 1
    monkeypatch.setattr(nostr_service, "_attach_media", no_media)
    monkeypatch.setattr(nostr_service.relay, "publish", publish)
    post = {"id": POST, "ref": "nostr:" + bech32.encode("npub", bytes.fromhex(author)),
            "note_ref": "nostr:" + bech32.encode("note", bytes.fromhex(POST))}
    text = bot.compose(post, 4, "This post had one job.")
    bot._post(text, [(b"x", "image/png")])
    (relays, ev), = got
    assert relays == nostr._RELAYS and all("127.0.0.1" in r or "localhost" in r for r in relays), relays
    assert ["p", author] in ev["tags"], ev["tags"]
    assert post["ref"] in ev["content"] and "Bad Post of the Day" in ev["content"]


# ---- the roast ------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "This post is so bad @alice@poster.place should log off.",           # names somebody
    "Peak nostr:npub1xyz energy.",
    "What a retarded take.",                                             # slur
    "Only a gay man would write this.",                                  # protected trait
    "Typical immigrant logic.",
    "Whoever wrote this should die.",                                    # threat
    "I'll find where you live and explain it slowly.",
    "x" * 600,                                                           # an essay, not a roast
])
def test_the_guard_refuses_a_roast_that_goes_after_the_person(bot, bad):
    assert not bot.roast_ok(bad)


def test_a_refused_roast_is_replaced_and_a_clean_one_kept(bot, monkeypatch):
    monkeypatch.setattr(bot, "_ai_on", lambda: True)
    asked = []
    monkeypatch.setattr(bot, "generate_reply", lambda p: asked.append(p) or "Whoever wrote this should die.")
    out = bot.roast({"id": POST, "text": "pineapple on everything"})
    assert out in bot.FALLBACK_ROASTS
    assert "no slurs" in asked[0] and "no threats" in asked[0] and "roast the post, never the person" in asked[0]
    monkeypatch.setattr(bot, "generate_reply", lambda p: "A take so lukewarm the microwave filed a complaint.")
    assert bot.roast({"id": POST, "text": "x"}) == "A take so lukewarm the microwave filed a complaint."


# ---- the relay side -------------------------------------------------------------------------------

def test_only_wellformed_votes_count_and_unreadable_is_not_empty(monkeypatch):
    from app.services import community_stats as cs
    voter, bot_pk, now = "e5" * 32, "f6" * 32, 1_800_000_000
    good = [["L", "pcai.badpost"], ["l", "bad-post-of-the-day", "pcai.badpost"], ["e", POST], ["p", AUTHOR]]
    evs = [{"id": "1" * 64, "pubkey": voter, "kind": 1985, "created_at": now, "tags": good},
           {"id": "2" * 64, "pubkey": AUTHOR, "kind": 1985, "created_at": now, "tags": good},       # own post
           {"id": "3" * 64, "pubkey": bot_pk, "kind": 1985, "created_at": now, "tags": good},       # a bot
           {"id": "4" * 64, "pubkey": voter, "kind": 1985, "created_at": now,
            "tags": [["L", "pcai.badpost"], ["l", "other", "pcai.badpost"], ["e", POST], ["p", AUTHOR]]},
           {"id": "5" * 64, "pubkey": voter, "kind": 1985, "created_at": now, "tags": good[:3]}]  # no author
    asked = []

    async def query(filters):
        asked.append(filters)
        return list(evs)
    monkeypatch.setattr(cs, "_query", query)
    monkeypatch.setattr(cs, "bot_pubkeys", lambda: {bot_pk})
    got = asyncio.run(cs.badpost_votes(now - 10, now + 10))
    assert [(v["voter"], v["post"], v["author"]) for v in got] == [(voter, POST, AUTHOR)], got
    assert asked[0][0]["#L"] == ["pcai.badpost"] and asked[0][0]["kinds"] == [1985]
    assert asked[0][0]["since"] == now - 10 and asked[0][0]["until"] == now + 9

    async def down(filters):
        raise ConnectionError("relay down")
    monkeypatch.setattr(cs, "_query", down)
    from fastapi import HTTPException
    from app.routers import community
    with pytest.raises(HTTPException) as e:
        asyncio.run(community.badpost_votes(since=1, until=2, _auth=True))
    assert e.value.status_code == 503, "an unreadable relay answered as 'nobody voted'"


def test_the_card_is_the_apps_own_post_card_renderer(monkeypatch):
    """The screenshot comes from `_render_post_card_png` (the ⋯ → Screenshot renderer), never a second
    one; a post the relay no longer holds is `post: null`, and a render failure still returns the post."""
    from app.routers import community
    from app.services import community_stats as cs
    import app.services.command_service as command_service
    post = {"id": POST, "pubkey": AUTHOR, "text": "pineapple", "created_at": 1_800_000_000, "handle": "@x@y",
            "ref": "nostr:npub1x", "note_ref": "nostr:note1x", "name": "X", "picture": "", "image": ""}
    held = {POST: post}

    async def one(i):
        return held.get(i)
    monkeypatch.setattr(cs, "badpost_post", one)
    calls = []
    monkeypatch.setattr(command_service, "_render_post_card_png",
                        lambda *a: calls.append(a) or b"\x89PNGrendered")
    got = asyncio.run(community.badpost_card(id=POST, _auth=True))
    assert got["post"]["id"] == POST and base64.b64decode(got["card"]) == b"\x89PNGrendered"
    assert calls and calls[0][0] == "X" and calls[0][2] == "pineapple"
    assert asyncio.run(community.badpost_card(id=POST2, _auth=True)) == {"post": None, "card": None}

    def boom(*a):
        raise RuntimeError("no browser")
    monkeypatch.setattr(command_service, "_render_post_card_png", boom)
    got = asyncio.run(community.badpost_card(id=POST, _auth=True))
    assert got["post"]["id"] == POST and got["card"] is None


# ---- dispatch -------------------------------------------------------------------------------------

def test_main_parses_and_a_nostr_badpost_preview_says_it_could_not_ask():
    from tests.test_community_bots import _run_bot
    r = _run_bot("--help")
    assert r.returncode == 0 and "--badpost" in r.stdout and "--badpost-print" in r.stdout, r.stderr[-2000:]
    r = _run_bot("--badpost-print", env={"NOSTR_NSEC": "11" * 32, "POSTERCHANAI_API_ENDPOINT": "http://127.0.0.1:9"})
    out = r.stdout + r.stderr
    assert "psycopg" not in out and "PLEROMA" not in out, out[-2000:]
    assert "could not read the bad post votes" in out.lower(), out[-2000:]


def test_the_badpost_daemon_starts_alongside_the_others(monkeypatch):
    import threading
    import types
    monkeypatch.syspath_prepend(HERE)
    monkeypatch.setenv("NOSTR_NSEC", "11" * 32)
    monkeypatch.delenv("PLEROMA_ENDPOINT", raising=False)
    for m in ("main", "config"):
        sys.modules.pop(m, None)
    ran, lock = [], threading.Lock()
    for name in ("nostr_badpostbot", "nostr_reportbot"):
        mod = types.ModuleType(name)
        mod.waitToStart = lambda: None
        mod.background = lambda name=name: (lock.acquire(), ran.append(name), lock.release())
        monkeypatch.setitem(sys.modules, name, mod)
    nk = types.ModuleType("nostr")
    nk.ensure_profile = nk.ensure_server_list = lambda: None
    monkeypatch.setitem(sys.modules, "nostr", nk)
    import main
    monkeypatch.setattr(sys, "argv", ["main.py", "--report", "--badpost"])
    main.main()
    assert sorted(ran) == ["nostr_badpostbot", "nostr_reportbot"], ran
