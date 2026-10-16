"""A web-of-trust rebuild keeps the members admitted WHILE it was crawling.

"shaniqua bot is not doing anything" (2026-10-16). Creating a bot publishes the operator's follow of it, admits it
at once (`wot-add`, 08:36:32) and asks for a full rebuild. The rebuild crawls follow lists from UPSTREAM relays --
which do not have that follow yet -- for minutes, then REPLACED the whole member set with what it crawled
(08:43:07). The bot was dropped: every event it published (its profile, its reply to a mention, its posts) was
refused "blocked: not in web of trust" for an hour, while the bot process logged "posted note". Runs the shipped
WotGate.build with a crawl that is still running when the bot is admitted.
"""
import asyncio
from collections import Counter
from unittest import mock

from app.services.nostr_relay import wot as wot_mod

SEED, FRIEND, BOT, BLOCKED = "1" * 64, "2" * 64, "b" * 64, "e" * 64


def _run(admit_during=(), blocked=()):
    gate = wot_mod.WotGate()
    gate.set_blocked(list(blocked))
    store = mock.Mock()
    store.wot_replace = mock.AsyncMock(return_value=0)
    store.kv_set = mock.AsyncMock()

    async def counter(self, upstream, authors, direct, batch, pace):
        gate.add_members(list(admit_during))          # admitted while the crawl is in flight
        await asyncio.sleep(0)
        return Counter({FRIEND: 1}) if SEED in authors else Counter()

    with mock.patch.object(wot_mod.WotGate, "_follows_counter", counter):
        asyncio.run(gate.build(store, [], [SEED], depth=1))
    return gate, store


def test_a_bot_admitted_while_the_rebuild_crawls_is_still_a_member_after_it():
    gate, store = _run(admit_during=[BOT])
    assert gate.is_member(BOT), "the rebuild threw out a member admitted while it ran"
    assert gate.is_member(FRIEND) and gate.is_member(SEED)
    persisted = store.wot_replace.await_args.args[0]
    assert BOT in persisted, "the persisted set (what the next start loads) dropped it too"


def test_a_member_added_before_the_rebuild_is_still_decided_by_the_crawl():
    gate = wot_mod.WotGate()
    gate.add_members([BOT])                            # long before: the crawl's answer stands
    gate._added_at[BOT] -= 3600
    store = mock.Mock()
    store.wot_replace = mock.AsyncMock(return_value=0)
    store.kv_set = mock.AsyncMock()

    async def counter(self, upstream, authors, direct, batch, pace):
        return Counter({FRIEND: 1}) if SEED in authors else Counter()
    with mock.patch.object(wot_mod.WotGate, "_follows_counter", counter):
        asyncio.run(gate.build(store, [], [SEED], depth=1, min_keep_ratio=0))
    assert not gate.is_member(BOT)


def test_a_blocked_account_is_not_kept_by_this():
    gate, _ = _run(admit_during=[BLOCKED], blocked=[BLOCKED])
    assert not gate.is_member(BLOCKED)


def test_a_bot_whose_post_no_relay_took_says_so_instead_of_posted_note(monkeypatch, capsys):
    """The other half of the hour nobody noticed: the bot printed "[nostr] posted note" / "replied to" while the
    relay refused every event. A send that no relay accepted is now reported as a failure."""
    import importlib, os, sys
    from app.services.nostr import nostr_service, relay
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    monkeypatch.setenv("NOSTR_NSEC", "1" * 64)
    monkeypatch.syspath_prepend(os.path.join(root, "botframework"))
    monkeypatch.chdir(os.path.join(root, "botframework"))
    sys.modules.pop("nostr", None)
    bot = importlib.import_module("nostr")
    # the key is read from the bot's config at import, and another test may have imported that config without one
    # (a parallel shard): set it on the module, which is what every send reads
    monkeypatch.setattr(bot, "_SECKEY", bytes.fromhex("1" * 64))
    monkeypatch.setattr(bot, "_RELAYS", ["ws://127.0.0.1:9"])

    async def refused(relays, ev, direct=False):
        return 0
    monkeypatch.setattr(relay, "publish", refused)
    bot.post_image_to_fediverse("hello")
    bot.send_reply({"_event": {"id": "a" * 64, "pubkey": "c" * 64, "kind": 1, "tags": []}}, "hi")
    out = capsys.readouterr().out
    assert "posted note" not in out and "replied to" not in out, out
    assert out.count("no relay accepted") == 2, out
    sys.modules.pop("nostr", None)
