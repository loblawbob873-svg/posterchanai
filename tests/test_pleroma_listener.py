"""The Pleroma reply bot (botframework/pleromaListener.py) answers mentions correctly.

Run: venv-unified/bin/python -m pytest tests/test_pleroma_listener.py

It had no tests. Pleroma is faked at the HTTP layer -- `requests.get/post` answer like a Mastodon API
-- so both the listener AND the real client (`pleroma.py`: mention prefix, visibility, media upload,
thread walk) are the shipped code. Only the expensive back ends (the LLM, image generation, the media
tools) are stubbed, at the names the listener calls.

What a person sees is what is asserted: whether a reply was posted, where it threads, who it
addresses, whether a private message stayed private, and that nobody is answered twice -- not across
polls, not across a restart, not across two processes of the same account.
"""
import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOTS = os.path.join(ROOT, "botframework")
BASE = "https://pleroma.test"
BOT = "posterchan"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class _R:
    def __init__(self, status=200, js=None, content=b""):
        self.status_code, self._js, self.content = status, js, content
        self.text = json.dumps(js) if js is not None else content.decode("latin1")

    def json(self):
        if self._js is None:
            raise json.JSONDecodeError("not json", self.text, 0)
        return self._js


class FakePleroma:
    """Just enough of the Mastodon API for the bot: who am I, notifications, statuses, media."""

    def __init__(self):
        self.statuses, self.notifications, self.posts, self.uploads, self.files = {}, [], [], [], {}
        self.refuse_posts = 0          # answer the next N status POSTs with a 500
        self.notifications_garbage = False
        self._n = 0

    def status(self, acct, text, *, visibility="public", reply_to=None, mentions=(), media=(), minutes_ago=0):
        self._n += 1
        sid = f"s{self._n:04d}"
        st = {"id": sid, "content": f"<p>{text}</p>", "visibility": visibility,
              "in_reply_to_id": reply_to, "account": {"acct": acct, "avatar": ""},
              "mentions": [{"acct": m} for m in mentions],
              "media_attachments": [{"type": "image", "url": u} for u in media],
              "created_at": _ts(minutes_ago)}
        self.statuses[sid] = st
        return st

    def mention(self, acct, text, **kw):
        """`acct` posts `text` addressing the bot, and Pleroma notifies the bot of it."""
        kw.setdefault("mentions", (BOT,))
        st = self.status(acct, text, **kw)
        self.notifications.insert(0, {"id": "n" + st["id"], "type": "mention", "status": st,
                                      "created_at": st["created_at"]})
        return st

    # ---- the HTTP surface ----------------------------------------------------------------------
    def get(self, url, headers=None, timeout=None, **kw):
        path = url[len(BASE):] if url.startswith(BASE) else url
        if path == "/api/v1/accounts/verify_credentials":
            return _R(js={"acct": BOT, "avatar": ""})
        if path == "/api/v1/notifications":
            return _R(content=b"<html>502</html>") if self.notifications_garbage else _R(js=self.notifications)
        if path.startswith("/api/v1/statuses/"):
            st = self.statuses.get(path.rsplit("/", 1)[1])
            return _R(js=st) if st else _R(404, js={"error": "not found"})
        if url in self.files:
            return _R(content=self.files[url])
        raise requests.ConnectionError(url)

    def post(self, url, headers=None, data=None, files=None, timeout=None, **kw):
        path = url[len(BASE):]
        if path in ("/api/v2/media", "/api/v1/media"):
            name, stream, mime = files["file"]
            self.uploads.append((name, stream.read(), mime))
            return _R(js={"id": f"m{len(self.uploads)}"})
        if path == "/api/v1/statuses":
            if self.refuse_posts:
                self.refuse_posts -= 1
                return _R(500, js={"error": "internal"})
            form = dict(data)
            form["media_ids"] = [v for k, v in data if k == "media_ids[]"]
            self.posts.append(form)
            return _R(js={"id": f"r{len(self.posts)}", "mentions": []})
        raise requests.ConnectionError(url)


def _ts(minutes_ago=0):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


class Bot:
    """The listener as it runs in production, one `poll()` per 20-second tick."""

    def __init__(self, monkeypatch, tmp_path, server):
        self.mp, self.tmp, self.server = monkeypatch, tmp_path, server
        self.replies_asked, self.images_asked, self.media_jobs = [], [], []
        monkeypatch.syspath_prepend(BOTS)
        for k, v in dict(PLEROMA_ENDPOINT=BASE, PLEROMA_USERNAME=BOT, PLEROMA_ACCESS_TOKEN="tok",
                         TIMEZONE="UTC", AUTO_NARRATE="false", PLEROMA_USE_APP_SERVICE="").items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(requests, "get", server.get)
        monkeypatch.setattr(requests, "post", server.post)
        self.start()

    def start(self):
        """(Re)start the process: fresh module state, the same state files on disk."""
        for m in ("config", "pleroma", "pleromaListener"):
            sys.modules.pop(m, None)
        L = importlib.import_module("pleromaListener")
        ids = str(self.tmp / "processed_ids")
        self.mp.setattr(L, "_PROCESSED_IDS_FILE", ids)
        self.mp.setattr(L, "_LOCK_FILE", ids + ".lock")
        L._processed_notification_ids.clear()
        L._replied_status_ids.clear()
        L._load_processed_ids()
        self.mp.setattr(L, "time", _NoSleep(L.time))
        self.mp.setattr(L, "generate_reply", self._reply)
        self.mp.setattr(L, "generate_image", lambda p: self.images_asked.append(p) or PNG)
        self.mp.setattr(L, "process_media", self._media)
        self.mp.setattr(L, "fetch_ytdl_media", lambda url, **kw: (b"\x00\x00\x00\x18ftypmp42", "video/mp4", None))
        self.L = L

    def _reply(self, prompt, thread_history=None, **kw):
        self.replies_asked.append((prompt, thread_history))
        return f"answer to: {prompt}"

    def _media(self, command, arg, media, handle, avatar):
        self.media_jobs.append((command, arg, [m[1] for m in media]))
        return "done", [{"filename": "out.png", "data": PNG, "content_type": "image/png"}]

    def poll(self, n=1):
        for _ in range(n):
            self.L.process_notifications()

    def replies_to(self, st):
        return [p for p in self.server.posts if p["in_reply_to_id"] == st["id"]]


class _NoSleep:
    """The listener's `time`, minus the minute-long politeness sleeps (news) -- nothing else changes."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, k):
        return getattr(self._real, k)

    def sleep(self, s):
        pass


@pytest.fixture
def world(monkeypatch, tmp_path):
    server = FakePleroma()
    return server, Bot(monkeypatch, tmp_path, server)


# ============================== answering, once ======================================================

def test_a_mention_is_answered_once_in_its_own_thread(world):
    pl, bot = world
    st = pl.mention("alice", f"@{BOT} what is the capital of France?")
    bot.poll(3)
    [r] = bot.replies_to(st)
    assert r["status"].startswith("@alice ") and "answer to: what is the capital of France?" in r["status"]
    assert f"@{BOT}" not in r["status"], "the bot addressed itself"
    assert r["visibility"] == "public"
    assert len(pl.posts) == 1, "a mention was answered more than once"


def test_a_restart_does_not_answer_the_same_mention_again(world):
    pl, bot = world
    st = pl.mention("alice", f"@{BOT} hello")
    bot.poll()
    bot.start()
    bot.poll(2)
    assert len(bot.replies_to(st)) == 1, "a restarted bot re-answered a mention"


def test_a_mention_another_process_already_took_is_left_to_it(world):
    """Two processes of one account share the claim file; the one that claimed a status answers it."""
    pl, bot = world
    st = pl.mention("alice", f"@{BOT} hello")
    with open(bot.L._PROCESSED_IDS_FILE, "a") as f:
        f.write(f"s:{st['id']}\n")
    bot.poll()
    assert pl.posts == []


def test_a_private_message_is_answered_privately_to_everyone_in_it(world):
    pl, bot = world
    st = pl.mention("alice", f"@{BOT} @carol secret plans", visibility="direct", mentions=(BOT, "carol"))
    bot.poll()
    [r] = bot.replies_to(st)
    assert r["visibility"] == "direct", "a direct message was answered in public"
    assert "@alice" in r["status"] and "@carol" in r["status"], "a participant was dropped from the DM"


def test_the_thread_is_given_to_the_model_oldest_first(world):
    pl, bot = world
    root = pl.status("alice", "I like trains")
    mid = pl.status(BOT, "trains are great", reply_to=root["id"])
    st = pl.mention("alice", f"@{BOT} which one is fastest?", reply_to=mid["id"])
    bot.poll()
    _, history = bot.replies_asked[0]
    assert [h["content"] for h in history] == ["I like trains", "trains are great", f"@{BOT} which one is fastest?"]
    assert [h["is_bot"] for h in history] == [False, True, False]


# ============================== who is NOT answered ==================================================

def test_old_mentions_are_not_dug_up(world):
    pl, bot = world
    pl.mention("alice", f"@{BOT} from last week", minutes_ago=10)
    bot.poll()
    assert pl.posts == []


def test_only_a_post_addressed_to_the_bot_first_is_answered(world):
    pl, bot = world
    pl.mention("alice", f"@carol have you met @{BOT}?", mentions=("carol", BOT))
    bot.poll()
    assert pl.posts == [], "the bot butted into a conversation addressed to someone else"


def test_other_bots_and_itself_are_never_answered(world):
    """The bot-to-bot loop guard: a listed bot, the bot itself, or a post also pinging another bot."""
    pl, bot = world
    pl.mention("yenta", f"@{BOT} hello from a bot")
    pl.mention(BOT, f"@{BOT} my own post")
    pl.mention("alice", f"@{BOT} ask @yenta too")
    bot.poll()
    assert pl.posts == []


def test_an_empty_mention_is_not_answered(world):
    pl, bot = world
    pl.mention("alice", f"@{BOT}")
    bot.poll()
    assert pl.posts == [] and bot.replies_asked == []


# ============================== commands ==============================================================

def test_help_lists_the_commands(world):
    pl, bot = world
    st = pl.mention("alice", f"@{BOT} help")
    bot.poll()
    [r] = bot.replies_to(st)
    assert "search <q>" in r["status"] and bot.replies_asked == [], "help went to the model"


def test_geni_posts_the_generated_picture(world):
    pl, bot = world
    st = pl.mention("alice", f"@{BOT} geni a red fox in the snow")
    bot.poll()
    [r] = bot.replies_to(st)
    assert r["media_ids"] and pl.uploads[0][1] == PNG and pl.uploads[0][2] == "image/png"
    assert bot.images_asked and "red fox" in bot.images_asked[0]


def test_geni_refuses_a_prohibited_prompt_without_generating_anything(world):
    pl, bot = world
    st = pl.mention("alice", f"@{BOT} geni a child at the beach")
    bot.poll()
    [r] = bot.replies_to(st)
    assert "cannot generate" in r["status"] and bot.images_asked == [] and pl.uploads == []


def test_an_image_command_uses_the_picture_further_up_the_thread(world):
    """People reply to a picture (or to the bot's reply under it) with `meme <text>`; the picture is the
    one up the thread, and the answer is the picture alone, no caption."""
    pl, bot = world
    url = BASE + "/media/cat.png"
    pl.files[url] = PNG
    pic = pl.status("bob", "look at my cat", media=(url,))
    st = pl.mention("alice", f"@{BOT} meme when the food is late", reply_to=pic["id"])
    bot.poll()
    assert bot.media_jobs == [("meme", "when the food is late", [PNG])]
    [r] = bot.replies_to(st)
    assert r["media_ids"] and r["status"] == "@alice", r["status"]


def test_a_meme_with_prohibited_text_is_refused(world):
    pl, bot = world
    st = pl.mention("alice", f"@{BOT} meme child")
    bot.poll()
    [r] = bot.replies_to(st)
    assert "cannot add that text" in r["status"] and bot.media_jobs == []


def test_downloads_are_rate_limited_per_person(world):
    pl, bot = world
    a = pl.mention("alice", f"@{BOT} ytdl https://youtube.com/watch?v=1")
    b = pl.mention("alice", f"@{BOT} ytdl https://youtube.com/watch?v=2")
    c = pl.mention("carol", f"@{BOT} ytdl https://youtube.com/watch?v=3")
    bot.poll()
    waits = [p for p in pl.posts if "Please wait" in p["status"]]
    assert len(waits) == 1 and waits[0]["in_reply_to_id"] in (a["id"], b["id"]), pl.posts
    [rc] = bot.replies_to(c)
    assert rc["media_ids"], "one person's cooldown held up somebody else"


# ============================== when Pleroma misbehaves ===============================================

def test_a_garbage_notifications_answer_is_a_quiet_poll(world):
    """Pleroma behind a proxy can answer an HTML error page with a 200; that is 'nothing to do'."""
    pl, bot = world
    pl.mention("alice", f"@{BOT} hello")
    pl.notifications_garbage = True
    bot.poll()
    pl.notifications_garbage = False
    bot.poll()
    assert len(pl.posts) == 1, "the mention was lost or the poll crashed"


def test_a_reply_pleroma_refused_is_sent_on_the_next_poll(world):
    """The status is claimed before the reply goes out; a refused reply must not leave it claimed, or
    that person is silently never answered."""
    pl, bot = world
    st = pl.mention("alice", f"@{BOT} hello")
    pl.refuse_posts = 1
    bot.poll()
    assert pl.posts == []
    bot.poll(2)
    assert len(bot.replies_to(st)) == 1, "a reply Pleroma refused once was never sent"


def test_a_reply_that_did_go_out_is_not_retried(world):
    pl, bot = world
    st = pl.mention("alice", f"@{BOT} hello")
    bot.poll()
    bot.L._processed_notification_ids.clear()          # even if the in-memory memory is lost
    bot.poll()
    assert len(bot.replies_to(st)) == 1


# ============================== the opt-in app-service shim ===========================================

def test_the_shim_sends_an_image_only_reply_like_the_client_does(monkeypatch):
    """PLEROMA_USE_APP_SERVICE swaps pleroma.py for pleroma_shim.py. The shim's empty-reply guard never
    learned about image-only replies, so every `meme` and image effect was silently dropped there."""
    monkeypatch.syspath_prepend(BOTS)
    monkeypatch.setenv("PLEROMA_ENDPOINT", BASE)
    monkeypatch.setenv("PLEROMA_ACCESS_TOKEN", "tok")
    for m in ("config", "pleroma_shim"):
        sys.modules.pop(m, None)
    shim = importlib.import_module("pleroma_shim")
    sent = []

    async def post_status(endpoint, token, text, **kw):
        sent.append((text, kw))
        return {"id": "r1"}
    monkeypatch.setattr(shim._svc, "post_status", post_status)
    st = {"id": "s1", "account": {"acct": "alice"}, "mentions": []}
    ok = shim.send_reply(st, "", own_acct=BOT, image_bytes=[(PNG, "image/png")])
    assert sent and sent[0][0] == "@alice" and sent[0][1]["media"], "an image-only reply was dropped"
    assert ok is True
